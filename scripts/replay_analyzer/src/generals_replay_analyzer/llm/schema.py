"""Versioned packaged prompt loading and closed response validation."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from importlib import resources
from typing import TypeAlias, cast
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError, ValidationError  # type: ignore[import-untyped]

from generals_replay_analyzer.llm.evidence_bundle import EvidenceBundle

PROMPT_VERSION = "strategy-report-v1"
RESPONSE_SCHEMA_VERSION = "strategy-report-response-v1"
RESPONSE_SCHEMA_ID = "generals-replay-analyzer/strategy-report-response-v1"
PROMPT_SHA256 = "c2603f4f4cff1b3563801d83a6c2e567700eba4a86fe3af83524bcb98d6693d7"
RESPONSE_SCHEMA_SHA256 = "a758faf24d931094b18ade9cf37c49bbe032686f7499180872f1d7bd7669b76e"
MAX_RESPONSE_BYTES = 262144
MAX_RESPONSE_CLAIMS = 256
MAX_SCALAR_BYTES = 4096
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 16384

JSONScalar: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]


class ResponseValidationError(ValueError):
    """A stable response rejection without provider-controlled details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"ResponseValidationError(code={self.code!r})"


class FrozenJSONMapping(Mapping[str, object]):
    """Tuple-backed recursively immutable JSON object."""

    __slots__ = ("_items",)

    def __init__(self, value: Mapping[str, object]) -> None:
        self._items = tuple(sorted((key, _freeze(item)) for key, item in value.items()))

    def __getitem__(self, key: str) -> object:
        for name, value in self._items:
            if name == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def as_plain(self) -> dict[str, object]:
        return {key: _thaw(value) for key, value in self._items}


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return FrozenJSONMapping(cast(Mapping[str, object], value))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, FrozenJSONMapping):
        return value.as_plain()
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class PromptResource:
    version: str
    digest: str
    content: bytes
    text: str

    def __post_init__(self) -> None:
        if (
            type(self.version) is not str
            or self.version != PROMPT_VERSION
            or type(self.digest) is not str
            or self.digest != PROMPT_SHA256
            or type(self.content) is not bytes
            or type(self.text) is not str
            or hashlib.sha256(self.content).hexdigest() != self.digest
        ):
            raise ResponseValidationError("invalid_resource_metadata")
        try:
            decoded = self.content.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise ResponseValidationError("invalid_resource_metadata") from None
        if self.text != decoded or not self.text.strip():
            raise ResponseValidationError("invalid_resource_metadata")


@dataclass(frozen=True)
class ResponseSchemaResource:
    version: str
    digest: str
    content: bytes
    document: FrozenJSONMapping

    def __post_init__(self) -> None:
        if (
            type(self.version) is not str
            or self.version != RESPONSE_SCHEMA_VERSION
            or type(self.digest) is not str
            or self.digest != RESPONSE_SCHEMA_SHA256
            or type(self.content) is not bytes
            or type(self.document) is not FrozenJSONMapping
            or hashlib.sha256(self.content).hexdigest() != self.digest
        ):
            raise ResponseValidationError("invalid_resource_metadata")
        value = _decode_json(self.content)
        if not isinstance(value, dict) or self.document.as_plain() != value:
            raise ResponseValidationError("invalid_resource_metadata")


_VALIDATED_RESPONSE_AUTHORITY = object()


@dataclass(frozen=True, init=False)
class ValidatedResponse:
    schema_version: str
    document: FrozenJSONMapping
    canonical_json: bytes
    digest: str

    def __init__(
        self,
        schema_version: str,
        document: FrozenJSONMapping,
        canonical_json: bytes,
        digest: str,
        *,
        _authority: object,
    ) -> None:
        if _authority is not _VALIDATED_RESPONSE_AUTHORITY:
            raise TypeError("ValidatedResponse is created only by validate_response")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "document", document)
        object.__setattr__(self, "canonical_json", canonical_json)
        object.__setattr__(self, "digest", digest)


# TheSuperHackers @fix Leex 23/08/2026 Preserve pinned LLM resource identities across Windows Git checkouts. (#TBD)
def _canonicalize_text_resource(content: bytes) -> bytes:
    return content.replace(b"\r\n", b"\n")


def _resource_bytes(name: str, expected_digest: str) -> bytes:
    try:
        content = resources.files("generals_replay_analyzer").joinpath("data", name).read_bytes()
    except (FileNotFoundError, OSError):
        raise ResponseValidationError("resource_unavailable") from None
    content = _canonicalize_text_resource(content)
    if hashlib.sha256(content).hexdigest() != expected_digest:
        raise ResponseValidationError("resource_digest_mismatch")
    return content


def _unique_object(pairs: list[tuple[str, JSONValue]]) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {}
    for key, value in pairs:
        if key in result:
            raise ResponseValidationError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ResponseValidationError("nonfinite_number")


def _decode_json(content: bytes) -> JSONValue:
    try:
        text = content.decode("utf-8", errors="strict")
        return cast(
            JSONValue,
            json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant),
        )
    except ResponseValidationError:
        raise
    except UnicodeDecodeError:
        raise ResponseValidationError("invalid_utf8") from None
    except (json.JSONDecodeError, RecursionError):
        raise ResponseValidationError("invalid_json") from None


# TheSuperHackers @feature Leex 22/08/2026 Load the exact packaged prompt without checkout fallback. (#TBD)
def load_prompt() -> PromptResource:
    content = _resource_bytes("strategy-report-v1.txt", PROMPT_SHA256)
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ResponseValidationError("resource_invalid_utf8") from None
    if not text.strip():
        raise ResponseValidationError("resource_invalid_prompt")
    return PromptResource(PROMPT_VERSION, PROMPT_SHA256, content, text)


# TheSuperHackers @feature Leex 22/08/2026 Validate the exact packaged closed response schema. (#TBD)
def load_response_schema() -> ResponseSchemaResource:
    content = _resource_bytes("strategy-report-response-v1.schema.json", RESPONSE_SCHEMA_SHA256)
    value = _decode_json(content)
    if not isinstance(value, dict) or value.get("$id") != RESPONSE_SCHEMA_ID:
        raise ResponseValidationError("resource_schema_version_mismatch")
    try:
        Draft202012Validator.check_schema(value)
    except SchemaError:
        raise ResponseValidationError("resource_invalid_schema") from None
    return ResponseSchemaResource(
        RESPONSE_SCHEMA_VERSION,
        RESPONSE_SCHEMA_SHA256,
        content,
        FrozenJSONMapping(cast(dict[str, object], value)),
    )


def _validate_scalars(
    value: object,
    *,
    depth: int = 0,
    nodes: list[int] | None = None,
) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ResponseValidationError("response_oversize")
    budget = [0] if nodes is None else nodes
    budget[0] += 1
    if budget[0] > MAX_JSON_NODES:
        raise ResponseValidationError("response_oversize")
    if type(value) is float:
        number = value
        if not math.isfinite(number):
            raise ResponseValidationError("nonfinite_number")
        if number == 0.0 and math.copysign(1.0, number) < 0:
            raise ResponseValidationError("negative_zero")
    elif isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_SCALAR_BYTES:
            raise ResponseValidationError("response_oversize")
    elif type(value) is dict:
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise ResponseValidationError("invalid_json_key")
            _validate_scalars(key, depth=depth + 1, nodes=budget)
            _validate_scalars(item, depth=depth + 1, nodes=budget)
    elif type(value) is list:
        for item in cast(list[object], value):
            _validate_scalars(item, depth=depth + 1, nodes=budget)
    elif value is not None and type(value) not in (bool, int, str):
        raise ResponseValidationError("invalid_response_shape")


def _canonical_public_uuid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        return str(UUID(value)) == value
    except (AttributeError, TypeError, ValueError):
        return False


def _domain_validate(document: dict[str, JSONValue], bundle: EvidenceBundle) -> None:
    allowed_evidence = {public_id for claim in bundle.claims for public_id in claim.evidence_ids}
    claim_ids: set[str] = set()
    claim_count = 0
    for section in (
        "phase_assessments",
        "strategy_assessments",
        "comparative_observations",
        "strengths",
        "vulnerabilities",
        "uncertainty_notes",
    ):
        claims = cast(list[dict[str, JSONValue]], document[section])
        claim_count += len(claims)
        for claim in claims:
            claim_id = cast(str, claim["claim_id"])
            if not claim_id.strip() or claim_id != claim_id.strip():
                raise ResponseValidationError("response_text_invalid")
            if claim_id in claim_ids:
                raise ResponseValidationError("duplicate_claim_id")
            claim_ids.add(claim_id)
            citations = cast(list[str], claim["evidence_ids"])
            if len(citations) != len(set(citations)):
                raise ResponseValidationError("duplicate_citation")
            if any(not _canonical_public_uuid(item) or item not in allowed_evidence for item in citations):
                raise ResponseValidationError("unknown_evidence_id")
            if section in ("phase_assessments", "strategy_assessments"):
                text = cast(str, claim["assessment"])
                if not text.strip() or text != text.strip():
                    raise ResponseValidationError("response_text_invalid")
                window = cast(dict[str, JSONValue], claim["window"])
                if cast(int, window["frame_end"]) < cast(int, window["frame_start"]):
                    raise ResponseValidationError("unsupported_window")
                if section == "strategy_assessments":
                    label = cast(str, claim["strategy_label"])
                    if not label.strip() or label != label.strip():
                        raise ResponseValidationError("response_text_invalid")
            else:
                text = cast(str, claim["text"])
                if not text.strip() or text != text.strip():
                    raise ResponseValidationError("response_text_invalid")
    if claim_count > MAX_RESPONSE_CLAIMS:
        raise ResponseValidationError("response_oversize")


# TheSuperHackers @feature Leex 22/08/2026 Bind structured interpretation claims to supplied public evidence. (#TBD)
def validate_response(response: bytes | str | Mapping[str, object], bundle: EvidenceBundle) -> ValidatedResponse:
    """Validate schema, domains, citations, bounds, and canonical response bytes."""
    if isinstance(response, bytes):
        if len(response) > MAX_RESPONSE_BYTES:
            raise ResponseValidationError("response_oversize")
        value = _decode_json(response)
    elif isinstance(response, str):
        encoded = response.encode("utf-8")
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise ResponseValidationError("response_oversize")
        value = _decode_json(encoded)
    elif isinstance(response, Mapping):
        try:
            value = cast(JSONValue, dict(response))
        except Exception:  # noqa: BLE001 -- arbitrary Mapping implementations are untrusted.
            raise ResponseValidationError("invalid_response_shape") from None
    else:
        raise ResponseValidationError("invalid_response_type")
    if not isinstance(value, dict):
        raise ResponseValidationError("invalid_response_shape")
    _validate_scalars(value)
    summary = value.get("summary")
    if type(summary) is str and (not summary.strip() or summary != summary.strip()):
        raise ResponseValidationError("response_text_invalid")
    schema = load_response_schema().document.as_plain()
    try:
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)
    except ValidationError:
        raise ResponseValidationError("response_schema_invalid") from None
    document = value
    _domain_validate(document, bundle)
    try:
        canonical = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError):
        raise ResponseValidationError("invalid_response_shape") from None
    if len(canonical) > MAX_RESPONSE_BYTES:
        raise ResponseValidationError("response_oversize")
    frozen = FrozenJSONMapping(cast(dict[str, object], document))
    return ValidatedResponse(
        schema_version=RESPONSE_SCHEMA_VERSION,
        document=frozen,
        canonical_json=canonical,
        digest=hashlib.sha256(canonical).hexdigest(),
        _authority=_VALIDATED_RESPONSE_AUTHORITY,
    )
