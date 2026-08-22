"""Optional local interpretation orchestration and deterministic fallback contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
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
)
from generals_replay_analyzer.llm.schema import (
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

FallbackValue: TypeAlias = None | bool | int | float | str | tuple[object, ...] | FrozenJSONMapping
LLMStatus: TypeAlias = Literal["succeeded", "failed", "invalid", "unavailable"]

_CACHE_SCHEMA = "ollama-analysis-cache-v1"
_ENDPOINT_POLICY = "loopback-http-literal-v1"
_RAW_ASSET_KIND = "analysis_raw_response"
_RAW_MEDIA_TYPE = "application/json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


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


@dataclass(frozen=True)
class FallbackClaim:
    """One immutable deterministic claim returned unchanged on LLM failure."""

    claim_id: str
    kind: EvidenceKind
    value: object
    quality: EvidenceQuality
    quality_reason: str | None
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if isinstance(self.value, dict):
            object.__setattr__(self, "value", FrozenJSONMapping(self.value))
        if type(self.evidence_ids) is not tuple:
            raise ValueError("fallback evidence IDs must be a tuple")


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
            )
            for claim in bundle.claims
        )
    )


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
) -> tuple[str, str]:
    settings = _settings_document()
    settings_digest = _digest(settings)
    cache = {
        "cache_schema": _CACHE_SCHEMA,
        "provider": "ollama",
        "endpoint_policy": _ENDPOINT_POLICY,
        "model_name": model.name,
        "model_digest": model.digest,
        "prompt": {"version": prompt.version, "digest": prompt.digest},
        "response_schema": {"version": schema.version, "digest": schema.digest},
        "generation": settings["generation"],
        "evidence_bundle_digest": bundle.digest,
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
        fallback = _fallback(request.evidence_bundle)
        prompt = load_prompt()
        response_schema = load_response_schema()
        run_id = self._run_id_factory()
        _public_uuid(run_id, "run_id")
        self._create_run(request, run_id, prompt, response_schema)
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
            status: Literal["invalid", "unavailable"] = (
                "unavailable" if error.code == "evidence_bundle_oversize" else "invalid"
            )
            self._record_status(run_id, status=status, code=error.code)
            return AnalysisOutcome(run_id, status, error.code, False, fallback)
        if not request.allow_ollama:
            self._record_status(run_id, status="unavailable", code="llm_disabled")
            return AnalysisOutcome(run_id, "unavailable", "llm_disabled", False, fallback)
        if cancellation is not None and cancellation.is_set():
            self._record_status(run_id, status="unavailable", code="cancelled")
            return AnalysisOutcome(run_id, "unavailable", "cancelled", False, fallback)
        provider: OllamaProvider | None = None
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
            )
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
            self._record_status(run_id, status="unavailable", code="cancelled")
            return AnalysisOutcome(run_id, "unavailable", "cancelled", False, fallback)
        except ProviderError as error:
            provider_status = self._provider_failure_status(error.code)
            self._record_status(
                run_id,
                status=provider_status,
                code=error.code,
                model=None if provider is None else provider.resolved_model,
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
                )
                return AnalysisOutcome(run_id, "unavailable", "cancelled", False, fallback)
            except ProviderError as error:
                provider_status = self._provider_failure_status(error.code)
                self._record_terminal_raw(
                    run_id=run_id,
                    status=provider_status,
                    code=error.code,
                    model=result.model,
                    prompt=prompt,
                    response_schema=response_schema,
                    bundle=request.evidence_bundle,
                    raw=result.response_bytes,
                    attempts=attempts,
                )
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
                self._record_terminal_raw(
                    run_id=run_id,
                    status="invalid",
                    code="response_validation_failed",
                    model=result.model,
                    prompt=prompt,
                    response_schema=response_schema,
                    bundle=request.evidence_bundle,
                    raw=result.response_bytes,
                    attempts=attempts,
                )
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
        )
        try:
            stored = self._store.store_bytes(
                result.response_bytes,
                expected_sha256=result.response_digest,
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
        prompt: PromptResource,
        response_schema: ResponseSchemaResource,
    ) -> None:
        unresolved = ModelIdentity(self._settings.ollama_model, _unresolved_digest(self._settings.ollama_model))
        settings_digest, cache_key = _cache_identity(
            model=unresolved,
            prompt=prompt,
            schema=response_schema,
            bundle=request.evidence_bundle,
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
                prompt_version=prompt.version,
                prompt_digest=prompt.digest,
                response_schema_version=response_schema.version,
                response_schema_digest=response_schema.digest,
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
        raw: bytes,
        attempts: list[dict[str, object]],
    ) -> None:
        stored = self._store.store_bytes(raw)
        settings_digest, cache_key = _cache_identity(
            model=model,
            prompt=prompt,
            schema=response_schema,
            bundle=bundle,
        )
        session = self._session_factory()
        try:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            run = session.scalar(select(AnalysisRun).where(AnalysisRun.run_id == run_id))
            if run is None:
                raise ValueError("analysis run is unavailable for terminal diagnostics")
            asset = self._managed_asset(session, stored)
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
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

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
    ) -> ValidatedResponse:
        if (
            run.provider != "ollama"
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
                or evidence.replay_id != run.replay_id
                or evidence.schema_version != 1
                or evidence.public_id != _uuid(f"evidence:{source_key}")
                or row.public_id != _uuid(f"strategy-assessment:{source_key}")
                or row.method != "llm"
                or row.replay_id != run.replay_id
                or row.replay_player_id != run.replay_player_id
                or row.strategy_label != claim["strategy_label"]
                or row.phase != claim["phase"]
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
