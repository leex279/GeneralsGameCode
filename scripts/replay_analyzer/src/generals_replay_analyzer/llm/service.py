"""Optional local interpretation orchestration and deterministic fallback contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypeAlias, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db.models import (
    AnalysisRun,
    AssessmentEvidence,
    EvidenceItem,
    ManagedAsset,
    Replay,
    ReplayPlayer,
    StrategyAssessment,
)
from generals_replay_analyzer.features.evidence import thaw_canonical
from generals_replay_analyzer.llm.evidence_bundle import (
    EvidenceBundle,
    EvidenceBundleError,
    EvidenceKind,
    EvidenceQuality,
)
from generals_replay_analyzer.llm.ollama import OllamaProvider
from generals_replay_analyzer.llm.provider import (
    CancellationSignal,
    GenerationOptions,
    JSONValue,
    ModelIdentity,
    OllamaClientConfig,
    OllamaTransport,
    ProviderError,
    StructuredRequest,
    TransportHeaders,
    TransportResponse,
    is_literal_loopback_endpoint,
)
from generals_replay_analyzer.llm.schema import (
    PROMPT_SHA256,
    PROMPT_VERSION,
    RESPONSE_SCHEMA_SHA256,
    RESPONSE_SCHEMA_VERSION,
    FrozenJSONMapping,
    PromptResource,
    ResponseSchemaResource,
    ResponseValidationError,
    ValidatedResponse,
    load_prompt,
    load_response_schema,
    validate_response,
)
from generals_replay_analyzer.storage import (
    ContentAddressedStore,
    ContentCollisionError,
    ContentStorageError,
    StoredContent,
)

if os.name == "nt":
    import msvcrt
else:
    import fcntl

FallbackValue: TypeAlias = None | bool | int | float | str | tuple[object, ...] | FrozenJSONMapping
LLMStatus: TypeAlias = Literal["succeeded", "failed", "invalid", "unavailable"]

_CACHE_SCHEMA = "ollama-analysis-cache-v1"
_ENDPOINT_POLICY = "loopback-http-literal-v1"
_RAW_ASSET_KIND = "analysis_raw_response"
_RAW_MEDIA_TYPE = "application/json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FALLBACK_AUTHORITY = object()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _public_uuid(value: str, label: str) -> None:
    if type(value) is not str:
        raise ValueError(f"{label} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError(f"{label} must be a canonical UUID") from None
    if str(parsed) != value:
        raise ValueError(f"{label} must be a canonical UUID")


def _uuid(identity: str) -> str:
    return str(uuid5(NAMESPACE_URL, identity))


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
    except OSError:
        raise ContentStorageError("analysis response lock is unavailable") from None
    try:
        if os.name == "nt":
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]
    except OSError:
        try:
            handle.close()
        except OSError:
            pass
        raise ContentStorageError("analysis response lock is unavailable") from None
    try:
        yield
    finally:
        try:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
        except OSError:
            pass
        try:
            handle.close()
        except OSError:
            pass


@dataclass(frozen=True, init=False)
class FallbackClaim:
    """One immutable deterministic claim returned unchanged on LLM failure."""

    claim_id: str
    kind: EvidenceKind
    value: FallbackValue
    quality: EvidenceQuality
    quality_reason: str | None
    evidence_ids: tuple[str, ...]

    def __init__(
        self,
        claim_id: str,
        kind: EvidenceKind,
        value: object,
        quality: EvidenceQuality,
        quality_reason: str | None,
        evidence_ids: tuple[str, ...],
        *,
        _authority: object,
    ) -> None:
        if _authority is not _FALLBACK_AUTHORITY:
            raise TypeError("fallback claims require service authority")
        if type(evidence_ids) is not tuple:
            raise TypeError("fallback evidence IDs must be a tuple")
        frozen = FrozenJSONMapping({"value": value})["value"]
        object.__setattr__(self, "claim_id", claim_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "value", cast(FallbackValue, frozen))
        object.__setattr__(self, "quality", quality)
        object.__setattr__(self, "quality_reason", quality_reason)
        object.__setattr__(self, "evidence_ids", evidence_ids)


@dataclass(frozen=True)
class DeterministicFallback:
    """Public ORM-free projection preserved when optional interpretation is absent."""

    claims: tuple[FallbackClaim, ...]

    def __post_init__(self) -> None:
        if type(self.claims) is not tuple:
            raise ValueError("fallback claims must be a tuple")


@dataclass(frozen=True)
class AnalysisRequest:
    """Public service input containing only semantic public identities and evidence."""

    replay_public_id: str
    replay_player_public_id: str | None
    evidence_bundle: EvidenceBundle
    allow_ollama: bool = True

    def __post_init__(self) -> None:
        _public_uuid(self.replay_public_id, "replay_public_id")
        if self.replay_player_public_id is not None:
            _public_uuid(self.replay_player_public_id, "replay_player_public_id")
        if type(self.evidence_bundle) is not EvidenceBundle:
            raise TypeError("evidence_bundle must be an accepted EvidenceBundle")
        if self.evidence_bundle.replay_public_id != self.replay_public_id:
            raise ValueError("evidence bundle replay identity mismatch")
        if type(self.allow_ollama) is not bool:
            raise TypeError("allow_ollama must be boolean")


@dataclass(frozen=True)
class AnalysisOutcome:
    """Public interpretation outcome with a deterministic continuation value."""

    run_id: str
    llm_status: LLMStatus
    code: str
    cache_hit: bool
    fallback: DeterministicFallback
    validated_response: FrozenJSONMapping | None = None


def _fallback(bundle: EvidenceBundle) -> DeterministicFallback:
    return DeterministicFallback(
        tuple(
            FallbackClaim(
                claim.claim_id,
                claim.kind,
                thaw_canonical(claim.value),
                claim.quality,
                claim.quality_reason,
                claim.evidence_ids,
                _authority=_FALLBACK_AUTHORITY,
            )
            for claim in bundle.claims
        )
    )


def _citation_contract(citations: dict[str, EvidenceItem]) -> list[dict[str, object]]:
    return [
        {
            "public_id": public_id,
            "schema_version": citation.schema_version,
            "source_key": citation.source_key,
            "source_kind": citation.source_kind,
            "tier": citation.tier,
        }
        for public_id, citation in sorted(citations.items())
    ]


def _settings_document() -> dict[str, object]:
    return {
        "endpoint_policy": _ENDPOINT_POLICY,
        "generation": {"seed": 0, "stream": False, "temperature": 0},
    }


def _cache_identity(
    *,
    model: ModelIdentity,
    prompt: PromptResource,
    schema: ResponseSchemaResource,
    bundle: EvidenceBundle,
    replay_player_public_id: str | None,
) -> tuple[str, str]:
    return _cache_identity_components(
        model=model,
        prompt_version=prompt.version,
        prompt_digest=prompt.digest,
        response_schema_version=schema.version,
        response_schema_digest=schema.digest,
        bundle=bundle,
        replay_player_public_id=replay_player_public_id,
    )


def _cache_identity_components(
    *,
    model: ModelIdentity,
    prompt_version: str,
    prompt_digest: str,
    response_schema_version: str,
    response_schema_digest: str,
    bundle: EvidenceBundle,
    replay_player_public_id: str | None,
) -> tuple[str, str]:
    settings = _settings_document()
    settings_digest = _digest(settings)
    cache = {
        "cache_schema": _CACHE_SCHEMA,
        "provider": "ollama",
        "endpoint_policy": _ENDPOINT_POLICY,
        "model_name": model.name,
        "model_digest": model.digest,
        "prompt": {"version": prompt_version, "digest": prompt_digest},
        "response_schema": {"version": response_schema_version, "digest": response_schema_digest},
        "generation": settings["generation"],
        "evidence_bundle_digest": bundle.digest,
        "replay_player_public_id": replay_player_public_id,
        "settings_digest": settings_digest,
    }
    return settings_digest, _digest(cache)


def _unresolved_digest(model_name: str) -> str:
    return hashlib.sha256(("ollama-unresolved-model-digest-v1:" + model_name).encode("utf-8")).hexdigest()


class _HttpxResponseBody:
    """Single-owner streaming response body with explicit close propagation."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._response.aiter_bytes()

    async def aclose(self) -> None:
        await self._response.aclose()


class HttpxOllamaTransport:
    """HTTPX adapter fixed to one validated loopback Ollama client policy."""

    def __init__(
        self,
        client_config: OllamaClientConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if type(client_config) is not OllamaClientConfig:
            raise ValueError("HTTPX transport requires exact Ollama client config")
        if not is_literal_loopback_endpoint(client_config.endpoint):
            raise ValueError("Ollama endpoint must be literal loopback HTTP with an explicit port")
        self.client_config = client_config
        timeout = client_config.timeout
        self._client = httpx.AsyncClient(
            base_url=client_config.endpoint,
            timeout=httpx.Timeout(
                connect=timeout.connect,
                read=timeout.read,
                write=timeout.write,
                pool=timeout.pool,
            ),
            trust_env=client_config.trust_env,
            follow_redirects=client_config.follow_redirects,
            headers={"accept": "application/json", "accept-encoding": "identity"},
            transport=transport,
        )

    async def request(
        self,
        method: str,
        path: str,
        payload: JSONValue | None,
        *,
        client_config: OllamaClientConfig,
        cancellation: CancellationSignal | None,
    ) -> TransportResponse:
        if client_config != self.client_config:
            raise ProviderError("transport_config_mismatch")
        if (method, path) not in {("GET", "/api/tags"), ("POST", "/api/chat")}:
            raise ProviderError("transport_request_invalid")
        if cancellation is not None and cancellation.is_set():
            raise asyncio.CancelledError
        try:
            request = self._client.build_request(
                method,
                path,
                json=payload if payload is not None else None,
            )
            response = await self._client.send(request, stream=True)
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            raise TimeoutError from None
        except httpx.HTTPError:
            raise RuntimeError("httpx_transport_failure") from None
        raw_length = response.headers.get("content-length")
        try:
            content_length = None if raw_length is None else int(raw_length)
            headers = TransportHeaders(
                content_type=response.headers.get("content-type", ""),
                content_length=content_length,
                location=response.headers.get("location"),
            )
        except (TypeError, ValueError):
            await response.aclose()
            raise ProviderError("response_header_invalid") from None
        return TransportResponse(
            status_code=response.status_code,
            headers=headers,
            body=_HttpxResponseBody(response),
            client_config=self.client_config,
        )

    async def aclose(self) -> None:
        """Close the owned HTTPX client after the service request lifecycle."""
        await self._client.aclose()


# TheSuperHackers @feature Leex 22/08/2026 Keep optional interpretation behind an immutable fallback boundary. (#TBD)
class OllamaAnalysisService:
    """Persist validated optional interpretations without changing deterministic evidence."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: AnalyzerSettings,
        store: ContentAddressedStore,
        transport: OllamaTransport,
        run_id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._store = store
        self._transport = transport
        self._run_id_factory = run_id_factory or (lambda: str(uuid4()))
        self._clock = clock or (lambda: datetime.now(UTC))

    async def analyze(
        self,
        request: AnalysisRequest,
        cancellation: CancellationSignal | None,
    ) -> AnalysisOutcome:
        if type(request) is not AnalysisRequest:
            raise TypeError("request must be an exact AnalysisRequest")
        run_id = self._run_id_factory()
        _public_uuid(run_id, "run_id")
        fallback = DeterministicFallback(())
        try:
            EvidenceBundle(
                schema_version=request.evidence_bundle.schema_version,
                replay_public_id=request.evidence_bundle.replay_public_id,
                replay_sha256=request.evidence_bundle.replay_sha256,
                claims=request.evidence_bundle.claims,
                unknown_or_missing=request.evidence_bundle.unknown_or_missing,
                canonical_json=request.evidence_bundle.canonical_json,
                digest=request.evidence_bundle.digest,
            )
        except EvidenceBundleError as error:
            self._create_run(
                request,
                run_id,
                prompt_version=PROMPT_VERSION,
                prompt_digest=PROMPT_SHA256,
                response_schema_version=RESPONSE_SCHEMA_VERSION,
                response_schema_digest=RESPONSE_SCHEMA_SHA256,
            )
            status: Literal["invalid", "unavailable"] = (
                "unavailable" if error.code == "evidence_bundle_oversize" else "invalid"
            )
            if error.code == "evidence_bundle_oversize":
                try:
                    fallback = _fallback(request.evidence_bundle)
                except (AttributeError, TypeError, ValueError):
                    fallback = DeterministicFallback(())
            self._record_status(run_id, status=status, code=error.code)
            return AnalysisOutcome(run_id, status, error.code, False, fallback)
        fallback = _fallback(request.evidence_bundle)
        try:
            prompt = load_prompt()
            response_schema = load_response_schema()
        except ResponseValidationError as error:
            self._create_run(
                request,
                run_id,
                prompt_version=PROMPT_VERSION,
                prompt_digest=PROMPT_SHA256,
                response_schema_version=RESPONSE_SCHEMA_VERSION,
                response_schema_digest=RESPONSE_SCHEMA_SHA256,
            )
            self._record_status(run_id, status="unavailable", code=error.code)
            return AnalysisOutcome(run_id, "unavailable", error.code, False, fallback)
        self._create_run(
            request,
            run_id,
            prompt_version=prompt.version,
            prompt_digest=prompt.digest,
            response_schema_version=response_schema.version,
            response_schema_digest=response_schema.digest,
        )
        if not request.allow_ollama:
            self._record_status(run_id, status="unavailable", code="llm_disabled")
            return AnalysisOutcome(run_id, "unavailable", "llm_disabled", False, fallback)
        if cancellation is not None and cancellation.is_set():
            self._record_status(run_id, status="unavailable", code="cancelled")
            return AnalysisOutcome(run_id, "unavailable", "cancelled", False, fallback)
        provider: OllamaProvider | None = None
        resolved_model: ModelIdentity | None = None
        resolved_settings_digest: str | None = None
        resolved_cache_key: str | None = None
        try:
            provider = OllamaProvider(
                self._settings.ollama_url,
                self._settings.ollama_model,
                self._transport,
            )
            model = await provider.discover_model(cancellation)
            settings_digest, cache_key = _cache_identity(
                model=model,
                prompt=prompt,
                schema=response_schema,
                bundle=request.evidence_bundle,
                replay_player_public_id=request.replay_player_public_id,
            )
            resolved_model = model
            resolved_settings_digest = settings_digest
            resolved_cache_key = cache_key
            try:
                cached = self._reuse_cached_success(
                    request=request,
                    run_id=run_id,
                    model=model,
                    prompt=prompt,
                    response_schema=response_schema,
                    settings_digest=settings_digest,
                    cache_key=cache_key,
                    fallback=fallback,
                )
            except (
                ContentCollisionError,
                ContentStorageError,
                ResponseValidationError,
                ValueError,
            ):
                self._record_status(
                    run_id,
                    status="failed",
                    code="cache_validation_failed",
                    model=model,
                    settings_digest=settings_digest,
                    cache_key=cache_key,
                )
                return AnalysisOutcome(
                    run_id,
                    "failed",
                    "cache_validation_failed",
                    False,
                    fallback,
                )
            if cached is not None:
                return cached
            structured_request = StructuredRequest(
                prompt,
                response_schema,
                request.evidence_bundle,
                GenerationOptions(),
            )
            result = await provider.generate_structured(structured_request, cancellation)
        except asyncio.CancelledError:
            self._record_status(
                run_id,
                status="unavailable",
                code="cancelled",
                model=resolved_model,
                settings_digest=resolved_settings_digest,
                cache_key=resolved_cache_key,
            )
            return AnalysisOutcome(run_id, "unavailable", "cancelled", False, fallback)
        except ProviderError as error:
            provider_status = self._provider_failure_status(error.code)
            self._record_status(
                run_id,
                status=provider_status,
                code=error.code,
                model=resolved_model if provider is not None else None,
                settings_digest=resolved_settings_digest,
                cache_key=resolved_cache_key,
            )
            return AnalysisOutcome(run_id, provider_status, error.code, False, fallback)
        attempts: list[dict[str, object]] = []
        try:
            validated = validate_response(result.response_bytes, request.evidence_bundle)
            attempts.append({"attempt": 1, "byte_count": len(result.response_bytes), "code": "ok"})
        except ResponseValidationError as first_error:
            attempts.append(
                {
                    "attempt": 1,
                    "byte_count": len(result.response_bytes),
                    "code": first_error.code,
                }
            )
            try:
                repaired = await provider.generate_structured(
                    StructuredRequest(
                        prompt,
                        response_schema,
                        request.evidence_bundle,
                        GenerationOptions(),
                        repair_error_code=first_error.code,
                    ),
                    cancellation,
                )
            except asyncio.CancelledError:
                self._record_status(
                    run_id,
                    status="unavailable",
                    code="cancelled",
                    model=result.model,
                    settings_digest=resolved_settings_digest,
                    cache_key=resolved_cache_key,
                )
                return AnalysisOutcome(run_id, "unavailable", "cancelled", False, fallback)
            except ProviderError as error:
                provider_status = self._provider_failure_status(error.code)
                terminal_error = self._record_terminal_raw(
                    run_id=run_id,
                    status=provider_status,
                    code=error.code,
                    model=result.model,
                    prompt=prompt,
                    response_schema=response_schema,
                    bundle=request.evidence_bundle,
                    replay_player_public_id=request.replay_player_public_id,
                    raw=result.response_bytes,
                    attempts=attempts,
                )
                if terminal_error is not None:
                    return AnalysisOutcome(run_id, "failed", terminal_error, False, fallback)
                return AnalysisOutcome(run_id, provider_status, error.code, False, fallback)
            result = repaired
            try:
                validated = validate_response(result.response_bytes, request.evidence_bundle)
                attempts.append({"attempt": 2, "byte_count": len(result.response_bytes), "code": "ok"})
            except ResponseValidationError as second_error:
                attempts.append(
                    {
                        "attempt": 2,
                        "byte_count": len(result.response_bytes),
                        "code": second_error.code,
                    }
                )
                terminal_error = self._record_terminal_raw(
                    run_id=run_id,
                    status="invalid",
                    code="response_validation_failed",
                    model=result.model,
                    prompt=prompt,
                    response_schema=response_schema,
                    bundle=request.evidence_bundle,
                    replay_player_public_id=request.replay_player_public_id,
                    raw=result.response_bytes,
                    attempts=attempts,
                )
                if terminal_error is not None:
                    return AnalysisOutcome(run_id, "failed", terminal_error, False, fallback)
                return AnalysisOutcome(
                    run_id,
                    "invalid",
                    "response_validation_failed",
                    False,
                    fallback,
                )
        settings_digest, cache_key = _cache_identity(
            model=result.model,
            prompt=prompt,
            schema=response_schema,
            bundle=request.evidence_bundle,
            replay_player_public_id=request.replay_player_public_id,
        )
        try:
            with self._digest_guard(result.response_digest):
                stored = self._store.store_bytes(
                    result.response_bytes,
                    expected_sha256=result.response_digest,
                )
                try:
                    return self._persist_success(
                        request=request,
                        run_id=run_id,
                        model=result.model,
                        prompt=prompt,
                        response_schema=response_schema,
                        settings_digest=settings_digest,
                        cache_key=cache_key,
                        stored=stored,
                        validated=validated,
                        fallback=fallback,
                        attempts=attempts,
                    )
                except Exception:  # noqa: BLE001 -- persistence failures become closed diagnostics.
                    self._cleanup_unregistered_content(stored)
                    self._record_status(
                        run_id,
                        status="failed",
                        code="persistence_failed",
                        model=result.model,
                        settings_digest=settings_digest,
                        cache_key=cache_key,
                    )
                    return AnalysisOutcome(
                        run_id,
                        "failed",
                        "persistence_failed",
                        False,
                        fallback,
                    )
        except (ContentCollisionError, ContentStorageError):
            self._record_status(
                run_id,
                status="failed",
                code="raw_asset_collision",
                model=result.model,
                settings_digest=settings_digest,
                cache_key=cache_key,
            )
            return AnalysisOutcome(
                run_id,
                "failed",
                "raw_asset_collision",
                False,
                fallback,
            )

    def _reuse_cached_success(
        self,
        *,
        request: AnalysisRequest,
        run_id: str,
        model: ModelIdentity,
        prompt: PromptResource,
        response_schema: ResponseSchemaResource,
        settings_digest: str,
        cache_key: str,
        fallback: DeterministicFallback,
    ) -> AnalysisOutcome | None:
        with self._session_factory() as lookup:
            winner = lookup.scalar(
                select(AnalysisRun).where(
                    AnalysisRun.cache_key == cache_key,
                    AnalysisRun.status == "succeeded",
                )
            )
            if winner is None:
                return None
            if winner.raw_response_asset_id is None:
                raise ValueError("analysis cache raw asset linkage is absent")
            asset = lookup.get(ManagedAsset, winner.raw_response_asset_id)
            if asset is None or not _SHA256.fullmatch(asset.sha256):
                raise ValueError("analysis cache raw asset identity is invalid")
            digest = asset.sha256
        with self._digest_guard(digest):
            return self._reuse_cached_success_locked(
                request=request,
                run_id=run_id,
                model=model,
                prompt=prompt,
                response_schema=response_schema,
                settings_digest=settings_digest,
                cache_key=cache_key,
                fallback=fallback,
            )

    def _reuse_cached_success_locked(
        self,
        *,
        request: AnalysisRequest,
        run_id: str,
        model: ModelIdentity,
        prompt: PromptResource,
        response_schema: ResponseSchemaResource,
        settings_digest: str,
        cache_key: str,
        fallback: DeterministicFallback,
    ) -> AnalysisOutcome | None:
        session = self._session_factory()
        try:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            run = session.scalar(select(AnalysisRun).where(AnalysisRun.run_id == run_id))
            if run is None:
                raise ValueError("analysis run disappeared before cache lookup")
            winner = session.scalar(
                select(AnalysisRun).where(
                    AnalysisRun.cache_key == cache_key,
                    AnalysisRun.status == "succeeded",
                )
            )
            if winner is None:
                session.commit()
                return None
            cached = self._revalidate_success(
                session,
                winner,
                request,
                model,
                prompt,
                response_schema,
                settings_digest,
                cache_key,
                current_run=run,
            )
            run.model_name = model.name
            run.model_digest = model.digest
            run.settings_digest = settings_digest
            run.cache_key = cache_key
            run.status = "unavailable"
            run.diagnostics_json = {
                "code": "cache_reused",
                "endpoint_policy": _ENDPOINT_POLICY,
                "model_identity_status": "resolved",
                "reused_run_id": winner.run_id,
            }
            run.error_json = {"code": "cache_reused"}
            run.completed_at = self._clock()
            session.commit()
            return AnalysisOutcome(
                winner.run_id,
                "succeeded",
                "ok",
                True,
                fallback,
                cached.document,
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _create_run(
        self,
        request: AnalysisRequest,
        run_id: str,
        *,
        prompt_version: str,
        prompt_digest: str,
        response_schema_version: str,
        response_schema_digest: str,
    ) -> None:
        unresolved = ModelIdentity(self._settings.ollama_model, _unresolved_digest(self._settings.ollama_model))
        settings_digest, cache_key = _cache_identity_components(
            model=unresolved,
            prompt_version=prompt_version,
            prompt_digest=prompt_digest,
            response_schema_version=response_schema_version,
            response_schema_digest=response_schema_digest,
            bundle=request.evidence_bundle,
            replay_player_public_id=request.replay_player_public_id,
        )
        with self._session_factory() as session:
            replay = session.scalar(select(Replay).where(Replay.public_id == request.replay_public_id))
            if replay is None or replay.sha256 != request.evidence_bundle.replay_sha256:
                raise ValueError("analysis replay identity is unavailable")
            replay_player_id = None
            if request.replay_player_public_id is not None:
                player = session.scalar(
                    select(ReplayPlayer).where(
                        ReplayPlayer.public_id == request.replay_player_public_id,
                        ReplayPlayer.replay_id == replay.id,
                    )
                )
                if player is None:
                    raise ValueError("analysis replay player identity is unavailable")
                replay_player_id = player.id
            now = self._clock()
            row = AnalysisRun(
                run_id=run_id,
                replay_id=replay.id,
                replay_player_id=replay_player_id,
                provider="ollama",
                model_name=self._settings.ollama_model,
                model_digest=unresolved.digest,
                prompt_version=prompt_version,
                prompt_digest=prompt_digest,
                response_schema_version=response_schema_version,
                response_schema_digest=response_schema_digest,
                settings_digest=settings_digest,
                input_digest=request.evidence_bundle.digest,
                cache_key=cache_key,
                status="pending",
                diagnostics_json={
                    "code": "pending",
                    "endpoint_policy": _ENDPOINT_POLICY,
                    "model_identity_status": "unresolved",
                },
                created_at=now,
            )
            session.add(row)
            session.commit()
        self._after_pending(run_id)
        with self._session_factory() as session:
            pending_row = session.scalar(select(AnalysisRun).where(AnalysisRun.run_id == run_id))
            if pending_row is None or pending_row.status != "pending":
                raise ValueError("analysis run pending transition is unavailable")
            pending_row.status = "running"
            pending_row.diagnostics_json = {
                "code": "running",
                "endpoint_policy": _ENDPOINT_POLICY,
                "model_identity_status": "unresolved",
            }
            session.commit()

    def _after_pending(self, run_id: str) -> None:
        """Test seam after durable pending creation and before dispatch eligibility."""
        del run_id

    def _persist_success(
        self,
        *,
        request: AnalysisRequest,
        run_id: str,
        model: ModelIdentity,
        prompt: PromptResource,
        response_schema: ResponseSchemaResource,
        settings_digest: str,
        cache_key: str,
        stored: StoredContent,
        validated: ValidatedResponse,
        fallback: DeterministicFallback,
        attempts: list[dict[str, object]],
    ) -> AnalysisOutcome:
        session = self._session_factory()
        try:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            run = session.scalar(select(AnalysisRun).where(AnalysisRun.run_id == run_id))
            if run is None:
                raise ValueError("analysis run disappeared before persistence")
            replay = session.get(Replay, run.replay_id)
            if replay is None:
                raise ValueError("analysis replay disappeared before persistence")
            winner = session.scalar(
                select(AnalysisRun).where(
                    AnalysisRun.cache_key == cache_key,
                    AnalysisRun.status == "succeeded",
                )
            )
            if winner is not None:
                cached = self._revalidate_success(
                    session,
                    winner,
                    request,
                    model,
                    prompt,
                    response_schema,
                    settings_digest,
                    cache_key,
                    current_run=run,
                )
                run.model_name = model.name
                run.model_digest = model.digest
                run.settings_digest = settings_digest
                run.cache_key = cache_key
                run.status = "unavailable"
                run.diagnostics_json = {
                    "code": "cache_reused",
                    "endpoint_policy": _ENDPOINT_POLICY,
                    "model_identity_status": "resolved",
                    "reused_run_id": winner.run_id,
                }
                run.error_json = {"code": "cache_reused"}
                run.completed_at = self._clock()
                session.commit()
                return AnalysisOutcome(
                    winner.run_id,
                    "succeeded",
                    "ok",
                    True,
                    fallback,
                    cached.document,
                )
            asset = self._managed_asset(session, stored)
            citations = self._authorized_citations(session, replay, request.evidence_bundle)
            self._insert_strategy_claims(
                session,
                replay,
                run,
                model,
                validated,
                request.evidence_bundle,
                citations,
            )
            session.flush()
            self._after_graph_insert(session)
            now = self._clock()
            run.model_name = model.name
            run.model_digest = model.digest
            run.prompt_version = prompt.version
            run.prompt_digest = prompt.digest
            run.response_schema_version = response_schema.version
            run.response_schema_digest = response_schema.digest
            run.settings_digest = settings_digest
            run.input_digest = request.evidence_bundle.digest
            run.cache_key = cache_key
            run.status = "succeeded"
            run.raw_response_asset_id = asset.id
            run.validated_response_json = validated.document.as_plain()
            run.diagnostics_json = {
                "attempts": attempts,
                "cache_schema": _CACHE_SCHEMA,
                "citations": _citation_contract(citations),
                "code": "ok",
                "endpoint_policy": _ENDPOINT_POLICY,
                "model_identity_status": "resolved",
            }
            run.error_json = None
            run.completed_at = now
            session.flush()
            session.commit()
            return AnalysisOutcome(
                run_id,
                "succeeded",
                "ok",
                False,
                fallback,
                validated.document,
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _provider_failure_status(
        code: str,
    ) -> Literal["failed", "unavailable"]:
        if code in {
            "invalid_endpoint",
            "model_unavailable",
            "model_digest_invalid",
            "discovery_envelope_invalid",
            "timeout",
        }:
            return "unavailable"
        return "failed"

    def _record_terminal_raw(
        self,
        *,
        run_id: str,
        status: Literal["failed", "invalid", "unavailable"],
        code: str,
        model: ModelIdentity,
        prompt: PromptResource,
        response_schema: ResponseSchemaResource,
        bundle: EvidenceBundle,
        replay_player_public_id: str | None,
        raw: bytes,
        attempts: list[dict[str, object]],
    ) -> str | None:
        digest = hashlib.sha256(raw).hexdigest()
        try:
            with self._digest_guard(digest):
                return self._record_terminal_raw_locked(
                    run_id=run_id,
                    status=status,
                    code=code,
                    model=model,
                    prompt=prompt,
                    response_schema=response_schema,
                    bundle=bundle,
                    replay_player_public_id=replay_player_public_id,
                    raw=raw,
                    attempts=attempts,
                )
        except (ContentCollisionError, ContentStorageError):
            settings_digest, cache_key = _cache_identity(
                model=model,
                prompt=prompt,
                schema=response_schema,
                bundle=bundle,
                replay_player_public_id=replay_player_public_id,
            )
            self._record_status(
                run_id,
                status="failed",
                code="raw_asset_collision",
                model=model,
                settings_digest=settings_digest,
                cache_key=cache_key,
            )
            return "raw_asset_collision"

    def _record_terminal_raw_locked(
        self,
        *,
        run_id: str,
        status: Literal["failed", "invalid", "unavailable"],
        code: str,
        model: ModelIdentity,
        prompt: PromptResource,
        response_schema: ResponseSchemaResource,
        bundle: EvidenceBundle,
        replay_player_public_id: str | None,
        raw: bytes,
        attempts: list[dict[str, object]],
    ) -> str | None:
        settings_digest, cache_key = _cache_identity(
            model=model,
            prompt=prompt,
            schema=response_schema,
            bundle=bundle,
            replay_player_public_id=replay_player_public_id,
        )
        try:
            stored = self._store.store_bytes(raw)
        except (ContentCollisionError, ContentStorageError):
            self._record_status(
                run_id,
                status="failed",
                code="raw_asset_collision",
                model=model,
                settings_digest=settings_digest,
                cache_key=cache_key,
            )
            return "raw_asset_collision"
        session = self._session_factory()
        try:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            run = session.scalar(select(AnalysisRun).where(AnalysisRun.run_id == run_id))
            if run is None:
                raise ValueError("analysis run is unavailable for terminal diagnostics")
            asset = self._managed_asset(session, stored)
            self._after_terminal_asset(session)
            run.model_name = model.name
            run.model_digest = model.digest
            run.settings_digest = settings_digest
            run.cache_key = cache_key
            run.status = status
            run.raw_response_asset_id = asset.id
            run.validated_response_json = None
            run.diagnostics_json = {
                "attempts": attempts,
                "code": code,
                "endpoint_policy": _ENDPOINT_POLICY,
                "model_identity_status": "resolved",
            }
            run.error_json = {"code": code}
            run.completed_at = self._clock()
            session.commit()
            return None
        except Exception:  # noqa: BLE001 -- terminal persistence failures are sanitized and reconciled.
            session.rollback()
            self._cleanup_unregistered_content(stored)
            self._record_status(
                run_id,
                status="failed",
                code="terminal_persistence_failed",
                model=model,
                settings_digest=settings_digest,
                cache_key=cache_key,
            )
            return "terminal_persistence_failed"
        finally:
            session.close()

    def _after_terminal_asset(self, session: Session) -> None:
        """Test seam after terminal asset registration and before commit."""
        del session

    @contextmanager
    def _digest_guard(self, digest: str) -> Iterator[None]:
        if type(digest) is not str or not _SHA256.fullmatch(digest):
            raise ContentStorageError("analysis response digest is invalid")
        lock_path = self._store.root.parent / ".llm-response-locks" / f"{digest}.lock"
        with _exclusive_file_lock(lock_path):
            yield

    def _cleanup_unregistered_content(self, stored: StoredContent) -> None:
        if not stored.created:
            return
        try:
            relative = stored.path.relative_to(self._store.root)
        except ValueError:
            return
        if relative.as_posix() != f"{stored.sha256[:2]}/{stored.sha256}":
            return
        try:
            with self._session_factory() as session:
                registered = session.scalar(select(ManagedAsset.id).where(ManagedAsset.sha256 == stored.sha256))
            if registered is None:
                stored.path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 -- rollback cleanup must never replace the typed analysis outcome.
            return

    def _revalidate_success(
        self,
        session: Session,
        run: AnalysisRun,
        request: AnalysisRequest,
        model: ModelIdentity,
        prompt: PromptResource,
        response_schema: ResponseSchemaResource,
        settings_digest: str,
        cache_key: str,
        *,
        current_run: AnalysisRun,
    ) -> ValidatedResponse:
        if (
            run.provider != "ollama"
            or run.replay_id != current_run.replay_id
            or run.replay_player_id != current_run.replay_player_id
            or run.model_name != model.name
            or run.model_digest != model.digest
            or run.prompt_version != prompt.version
            or run.prompt_digest != prompt.digest
            or run.response_schema_version != response_schema.version
            or run.response_schema_digest != response_schema.digest
            or run.settings_digest != settings_digest
            or run.input_digest != request.evidence_bundle.digest
            or run.cache_key != cache_key
            or run.status != "succeeded"
            or run.raw_response_asset_id is None
            or not isinstance(run.validated_response_json, dict)
            or run.error_json is not None
            or run.completed_at is None
        ):
            raise ValueError("analysis cache identity mismatch")
        asset = session.get(ManagedAsset, run.raw_response_asset_id)
        if asset is None or (
            asset.kind != _RAW_ASSET_KIND or asset.media_type != _RAW_MEDIA_TYPE or not _SHA256.fullmatch(asset.sha256)
        ):
            raise ContentCollisionError("analysis cache raw asset metadata mismatch")
        verified = self._store.verify(asset.sha256)
        try:
            expected_relative = verified.path.relative_to(self._settings.data_root).as_posix()
        except ValueError:
            raise ContentCollisionError("analysis cache raw asset is outside data root") from None
        if asset.relative_path != expected_relative or asset.size_bytes != verified.size:
            raise ContentCollisionError("analysis cache raw asset identity mismatch")
        raw = verified.path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != asset.sha256 or len(raw) != asset.size_bytes:
            raise ContentCollisionError("analysis cache raw asset bytes mismatch")
        validated = validate_response(raw, request.evidence_bundle)
        if validated.document.as_plain() != run.validated_response_json:
            raise ValueError("analysis cache validated response mismatch")
        replay = session.get(Replay, run.replay_id)
        if replay is None:
            raise ValueError("analysis cache replay is absent")
        citations = self._authorized_citations(session, replay, request.evidence_bundle)
        diagnostics = run.diagnostics_json
        if not isinstance(diagnostics, dict) or set(diagnostics) != {
            "attempts",
            "cache_schema",
            "citations",
            "code",
            "endpoint_policy",
            "model_identity_status",
        }:
            raise ValueError("analysis cache success diagnostics mismatch")
        attempts = diagnostics["attempts"]
        if not isinstance(attempts, list) or len(attempts) not in (1, 2):
            raise ValueError("analysis cache attempt diagnostics mismatch")
        for index, attempt in enumerate(attempts, start=1):
            if (
                not isinstance(attempt, dict)
                or set(attempt) != {"attempt", "byte_count", "code"}
                or attempt["attempt"] != index
                or type(attempt["byte_count"]) is not int
                or attempt["byte_count"] < 0
                or type(attempt["code"]) is not str
                or not attempt["code"]
            ):
                raise ValueError("analysis cache attempt diagnostics mismatch")
        if (
            cast(dict[str, object], attempts[-1])["code"] != "ok"
            or cast(dict[str, object], attempts[-1])["byte_count"] != len(raw)
            or diagnostics["cache_schema"] != _CACHE_SCHEMA
            or diagnostics["citations"] != _citation_contract(citations)
            or diagnostics["code"] != "ok"
            or diagnostics["endpoint_policy"] != _ENDPOINT_POLICY
            or diagnostics["model_identity_status"] != "resolved"
        ):
            raise ValueError("analysis cache success diagnostics mismatch")
        self._revalidate_graph(session, run, request.evidence_bundle, model, validated)
        return validated

    def _revalidate_graph(
        self,
        session: Session,
        run: AnalysisRun,
        bundle: EvidenceBundle,
        model: ModelIdentity,
        validated: ValidatedResponse,
    ) -> None:
        document = validated.document.as_plain()
        claims = cast(list[dict[str, object]], document["strategy_assessments"])
        rows = tuple(
            session.scalars(
                select(StrategyAssessment)
                .where(StrategyAssessment.analysis_run_id == run.id)
                .order_by(StrategyAssessment.public_id)
            ).all()
        )
        if len(rows) != len(claims):
            raise ValueError("analysis cache assessment graph is partial")
        rows_by_source: dict[str, tuple[StrategyAssessment, EvidenceItem]] = {}
        for row in rows:
            evidence = session.get(EvidenceItem, row.evidence_item_id)
            if evidence is None:
                raise ValueError("analysis cache inferred evidence is absent")
            rows_by_source[evidence.source_key] = (row, evidence)
        if len(rows_by_source) != len(rows):
            raise ValueError("analysis cache inferred evidence identity collides")
        quality_by_id = self._quality_by_evidence(bundle)
        for claim in claims:
            claim_id = cast(str, claim["claim_id"])
            source_key = f"analysis-run:{run.run_id}:{claim_id}"
            pair = rows_by_source.get(source_key)
            if pair is None:
                raise ValueError("analysis cache claim source identity mismatch")
            row, evidence = pair
            evidence_ids = cast(list[str], claim["evidence_ids"])
            quality, reasons = self._minimum_quality(evidence_ids, quality_by_id)
            window = cast(dict[str, int], claim["window"])
            if (
                evidence.tier != "inferred"
                or evidence.source_kind != "llm"
                or evidence.parser_run_id is not None
                or evidence.telemetry_run_id is not None
                or evidence.replay_id != run.replay_id
                or evidence.schema_version != 1
                or evidence.public_id != _uuid(f"evidence:{source_key}")
                or row.public_id != _uuid(f"strategy-assessment:{source_key}")
                or row.method != "llm"
                or row.replay_id != run.replay_id
                or row.replay_player_id != run.replay_player_id
                or row.strategy_label != claim["strategy_label"]
                or row.phase != claim["phase"]
                or row.taxonomy_version is not None
                or row.rule_version is not None
                or row.model_version != model.digest
                or row.frame_start != window["frame_start"]
                or row.frame_end != window["frame_end"]
                or row.quality != quality
                or row.confidence != claim["confidence"]
                or row.details_json
                != {
                    "assessment": claim["assessment"],
                    "claim_id": claim_id,
                    "cited_quality_reasons": list(reasons),
                    "minimum_cited_quality": {
                        "available": "complete",
                        "partial": "partial",
                        "unavailable": "unavailable",
                    }[quality],
                    "schema_version": "llm-strategy-assessment-v1",
                }
            ):
                raise ValueError("analysis cache assessment graph mismatch")
            linked = tuple(
                session.execute(
                    select(AssessmentEvidence.role, EvidenceItem.public_id)
                    .join(EvidenceItem, EvidenceItem.id == AssessmentEvidence.evidence_item_id)
                    .where(AssessmentEvidence.assessment_id == row.id)
                    .order_by(EvidenceItem.public_id)
                ).all()
            )
            if linked != tuple(("supporting", public_id) for public_id in sorted(evidence_ids)):
                raise ValueError("analysis cache citation graph mismatch")

    def _record_status(
        self,
        run_id: str,
        *,
        status: Literal["failed", "invalid", "unavailable"],
        code: str,
        model: ModelIdentity | None = None,
        settings_digest: str | None = None,
        cache_key: str | None = None,
    ) -> None:
        with self._session_factory() as session:
            run = session.scalar(select(AnalysisRun).where(AnalysisRun.run_id == run_id))
            if run is None:
                raise ValueError("analysis run is unavailable for diagnostics")
            if model is not None:
                run.model_name = model.name
                run.model_digest = model.digest
            if settings_digest is not None:
                run.settings_digest = settings_digest
            if cache_key is not None:
                run.cache_key = cache_key
            run.status = status
            run.diagnostics_json = {
                "code": code,
                "endpoint_policy": _ENDPOINT_POLICY,
                "model_identity_status": "resolved" if model is not None else "unresolved",
            }
            run.error_json = {"code": code}
            run.completed_at = self._clock()
            session.commit()

    def _managed_asset(self, session: Session, stored: StoredContent) -> ManagedAsset:
        try:
            relative = stored.path.relative_to(self._settings.data_root).as_posix()
        except ValueError:
            raise ContentCollisionError("managed response is outside the analyzer data root") from None
        existing = session.scalar(select(ManagedAsset).where(ManagedAsset.sha256 == stored.sha256))
        if existing is not None:
            if (
                existing.kind != _RAW_ASSET_KIND
                or existing.media_type != _RAW_MEDIA_TYPE
                or existing.relative_path != relative
                or existing.size_bytes != stored.size
            ):
                raise ContentCollisionError("managed response asset identity collision")
            verified = self._store.verify(stored.sha256)
            if verified.path != stored.path or verified.size != stored.size:
                raise ContentCollisionError("managed response asset bytes changed")
            return existing
        row = ManagedAsset(
            public_id=_uuid(f"managed-asset:{stored.sha256}"),
            sha256=stored.sha256,
            kind=_RAW_ASSET_KIND,
            relative_path=relative,
            size_bytes=stored.size,
            media_type=_RAW_MEDIA_TYPE,
            created_at=self._clock(),
        )
        session.add(row)
        session.flush()
        return row

    @staticmethod
    def _authorized_citations(
        session: Session,
        replay: Replay,
        bundle: EvidenceBundle,
    ) -> dict[str, EvidenceItem]:
        public_ids = tuple(sorted({public_id for claim in bundle.claims for public_id in claim.evidence_ids}))
        rows = {
            row.public_id: row
            for row in session.scalars(select(EvidenceItem).where(EvidenceItem.public_id.in_(public_ids))).all()
        }
        if set(rows) != set(public_ids):
            raise ValueError("analysis bundle cites unknown persistence evidence")
        if any(row.replay_id != replay.id or row.tier not in ("observed", "derived") for row in rows.values()):
            raise ValueError("analysis bundle evidence ownership mismatch")
        return rows

    def _insert_strategy_claims(
        self,
        session: Session,
        replay: Replay,
        run: AnalysisRun,
        model: ModelIdentity,
        validated: ValidatedResponse,
        bundle: EvidenceBundle,
        citations: dict[str, EvidenceItem],
    ) -> None:
        document = validated.document.as_plain()
        quality_by_id = self._quality_by_evidence(bundle)
        raw_claims = document["strategy_assessments"]
        if not isinstance(raw_claims, list):
            raise TypeError("validated strategy claims are unavailable")
        for raw in raw_claims:
            claim = cast(dict[str, object], raw)
            claim_id = cast(str, claim["claim_id"])
            evidence_ids = cast(list[str], claim["evidence_ids"])
            persistence_quality, reasons = self._minimum_quality(evidence_ids, quality_by_id)
            minimum_quality = {
                "available": "complete",
                "partial": "partial",
                "unavailable": "unavailable",
            }[persistence_quality]
            source_key = f"analysis-run:{run.run_id}:{claim_id}"
            evidence = EvidenceItem(
                public_id=_uuid(f"evidence:{source_key}"),
                replay_id=replay.id,
                tier="inferred",
                source_kind="llm",
                source_key=source_key,
                schema_version=1,
                created_at=self._clock(),
            )
            session.add(evidence)
            session.flush()
            window = cast(dict[str, int], claim["window"])
            assessment = StrategyAssessment(
                public_id=_uuid(f"strategy-assessment:{source_key}"),
                evidence_item_id=evidence.id,
                replay_id=replay.id,
                replay_player_id=run.replay_player_id,
                analysis_run_id=run.id,
                method="llm",
                strategy_label=cast(str, claim["strategy_label"]),
                phase=cast(str, claim["phase"]),
                taxonomy_version=None,
                rule_version=None,
                model_version=model.digest,
                frame_start=window["frame_start"],
                frame_end=window["frame_end"],
                quality=persistence_quality,
                confidence=cast(float, claim["confidence"]),
                details_json={
                    "assessment": claim["assessment"],
                    "claim_id": claim_id,
                    "cited_quality_reasons": list(reasons),
                    "minimum_cited_quality": minimum_quality,
                    "schema_version": "llm-strategy-assessment-v1",
                },
                created_at=self._clock(),
            )
            session.add(assessment)
            session.flush()
            for public_id in evidence_ids:
                session.add(
                    AssessmentEvidence(
                        assessment_id=assessment.id,
                        evidence_item_id=citations[public_id].id,
                        role="supporting",
                    )
                )

    @staticmethod
    def _quality_by_evidence(
        bundle: EvidenceBundle,
    ) -> dict[str, list[tuple[EvidenceQuality, str | None]]]:
        result: dict[str, list[tuple[EvidenceQuality, str | None]]] = {}
        for claim in bundle.claims:
            for public_id in claim.evidence_ids:
                result.setdefault(public_id, []).append((claim.quality, claim.quality_reason))
        return result

    @staticmethod
    def _minimum_quality(
        evidence_ids: list[str],
        quality_by_id: dict[str, list[tuple[EvidenceQuality, str | None]]],
    ) -> tuple[str, tuple[str, ...]]:
        cited = [quality for public_id in evidence_ids for quality in quality_by_id[public_id]]
        ranks = {"complete": 2, "partial": 1, "unavailable": 0}
        minimum = min((quality for quality, _ in cited), key=ranks.__getitem__)
        reasons = tuple(sorted({reason for quality, reason in cited if quality != "complete" and reason is not None}))
        return {
            "complete": "available",
            "partial": "partial",
            "unavailable": "unavailable",
        }[minimum], reasons

    def _after_graph_insert(self, session: Session) -> None:
        """Test seam after the complete inferred graph is pending before commit."""
        del session
