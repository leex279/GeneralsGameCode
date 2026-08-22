"""Loopback-only Ollama discovery and structured chat provider."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import cast

from generals_replay_analyzer.llm.provider import (
    CancellationSignal,
    JSONValue,
    ModelIdentity,
    OllamaClientConfig,
    OllamaTransport,
    ProviderError,
    ProviderResult,
    StructuredRequest,
    TransportResponse,
)

_ENDPOINT = re.compile(r"http://(?:(?:127\.0\.0\.1)|(?:\[::1\])):([1-9][0-9]{0,4})")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_HTTP_BYTES = 262144
_MAX_MODELS = 256
_MAX_TEXT_BYTES = 4096
_TAG_KEYS = {"name", "model", "modified_at", "size", "digest", "details"}
_DETAIL_KEYS = {"parent_model", "format", "family", "families", "parameter_size", "quantization_level"}
_CHAT_KEYS = {
    "model",
    "created_at",
    "message",
    "done",
    "done_reason",
    "total_duration",
    "load_duration",
    "prompt_eval_count",
    "prompt_eval_duration",
    "eval_count",
    "eval_duration",
}
_DURATION_KEYS = {
    "total_duration",
    "load_duration",
    "prompt_eval_count",
    "prompt_eval_duration",
    "eval_count",
    "eval_duration",
}


def _valid_endpoint(endpoint: str) -> bool:
    if type(endpoint) is not str:
        return False
    match = _ENDPOINT.fullmatch(endpoint)
    return match is not None and int(match.group(1)) <= 65535


def _short_text(value: object, *, allow_empty: bool = False) -> bool:
    return (
        type(value) is str
        and (allow_empty or bool(value))
        and len(value.encode("utf-8")) <= _MAX_TEXT_BYTES
    )


def _unique_object(pairs: list[tuple[str, JSONValue]]) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {}
    for key, value in pairs:
        if key in result:
            raise ProviderError("response_json_invalid")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ProviderError("response_json_invalid")


def _content_type_is_json(value: str) -> bool:
    parts = [part.strip().lower() for part in value.split(";")]
    if not parts or parts[0] != "application/json":
        return False
    return all(part == "charset=utf-8" for part in parts[1:])


async def _decode_transport_response(
    response: TransportResponse, expected_config: OllamaClientConfig
) -> JSONValue:
    if type(response) is not TransportResponse or response.client_config != expected_config:
        raise ProviderError("transport_config_mismatch")
    headers = response.headers
    if headers.location is not None:
        raise ProviderError("redirect_rejected")
    if 300 <= response.status_code <= 399:
        raise ProviderError("redirect_rejected")
    if response.status_code < 200 or response.status_code > 299:
        raise ProviderError("http_status_error")
    if not _content_type_is_json(headers.content_type):
        raise ProviderError("content_type_invalid")
    if headers.content_length is not None and headers.content_length > _MAX_HTTP_BYTES:
        raise ProviderError("response_oversize")
    chunks: list[bytes] = []
    size = 0
    try:
        async for chunk in response.body:
            if type(chunk) is not bytes:
                raise ProviderError("response_body_invalid")
            size += len(chunk)
            if size > _MAX_HTTP_BYTES:
                raise ProviderError("response_oversize")
            chunks.append(chunk)
    except ProviderError:
        raise
    except Exception:  # noqa: BLE001 -- the injected stream is an untrusted boundary.
        raise ProviderError("response_body_invalid") from None
    if headers.content_length is not None and size != headers.content_length:
        raise ProviderError("content_length_invalid")
    body = b"".join(chunks)
    try:
        text = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ProviderError("response_utf8_invalid") from None
    try:
        return cast(
            JSONValue,
            json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant),
        )
    except ProviderError:
        raise
    except (json.JSONDecodeError, RecursionError):
        raise ProviderError("response_json_invalid") from None


def _validate_details(value: object) -> bool:
    if not isinstance(value, dict) or set(value) - _DETAIL_KEYS:
        return False
    for key, item in value.items():
        if key == "families":
            if not isinstance(item, list) or len(item) > 32 or any(not _short_text(name) for name in item):
                return False
        elif not _short_text(item, allow_empty=True):
            return False
    return True


def _validate_tag_shape(value: object) -> bool:
    if not isinstance(value, dict) or set(value) - _TAG_KEYS:
        return False
    if "name" not in value or not _short_text(value["name"]):
        return False
    if "model" in value and not _short_text(value["model"]):
        return False
    if "modified_at" in value and not _short_text(value["modified_at"]):
        return False
    if "size" in value and (type(value["size"]) is not int or value["size"] < 0):
        return False
    if "digest" in value and type(value["digest"]) is not str:
        return False
    return "details" not in value or _validate_details(value["details"])


def _model_from_tags(value: JSONValue, configured_model: str) -> ModelIdentity:
    if not isinstance(value, dict) or set(value) != {"models"}:
        raise ProviderError("discovery_envelope_invalid")
    models = value["models"]
    if not isinstance(models, list) or len(models) > _MAX_MODELS or any(not _validate_tag_shape(item) for item in models):
        raise ProviderError("discovery_envelope_invalid")
    matches = [cast(dict[str, JSONValue], item) for item in models if cast(dict[str, JSONValue], item)["name"] == configured_model]
    if not matches:
        raise ProviderError("model_unavailable")
    if len(matches) != 1:
        raise ProviderError("discovery_envelope_invalid")
    digest = matches[0].get("digest")
    if type(digest) is not str or not _SHA256.fullmatch(digest):
        raise ProviderError("model_digest_invalid")
    return ModelIdentity(configured_model, digest)


def _chat_content(value: JSONValue, model: ModelIdentity) -> bytes:
    if not isinstance(value, dict) or set(value) - _CHAT_KEYS:
        raise ProviderError("chat_envelope_invalid")
    if not {"model", "message", "done"} <= set(value):
        raise ProviderError("chat_envelope_invalid")
    if value["model"] != model.name or value["done"] is not True:
        raise ProviderError("chat_envelope_invalid")
    for key in ("created_at", "done_reason"):
        if key in value and not _short_text(value[key], allow_empty=True):
            raise ProviderError("chat_envelope_invalid")
    for key in _DURATION_KEYS:
        if key in value and (type(value[key]) is not int or cast(int, value[key]) < 0):
            raise ProviderError("chat_envelope_invalid")
    message = value["message"]
    if not isinstance(message, dict) or set(message) - {"role", "content", "thinking"}:
        raise ProviderError("chat_envelope_invalid")
    if set(message) < {"role", "content"} or message["role"] != "assistant" or type(message["content"]) is not str:
        raise ProviderError("chat_envelope_invalid")
    if "thinking" in message and not _short_text(message["thinking"], allow_empty=True):
        raise ProviderError("chat_envelope_invalid")
    content = message["content"].encode("utf-8")
    if len(content) > _MAX_HTTP_BYTES:
        raise ProviderError("response_oversize")
    return content


# TheSuperHackers @feature Leex 22/08/2026 Restrict optional Ollama calls to exact loopback endpoints. (#TBD)
class OllamaProvider:
    """One-discovery, one-chat provider with no transport retry."""

    def __init__(self, endpoint: str, model_name: str, transport: OllamaTransport) -> None:
        if not _valid_endpoint(endpoint):
            raise ProviderError("invalid_endpoint")
        if type(model_name) is not str or not model_name or model_name != model_name.strip() or len(model_name.encode()) > 255:
            raise ProviderError("invalid_model_name")
        self._model_name = model_name
        self._transport = transport
        self._resolved_model: ModelIdentity | None = None
        self.client_config = OllamaClientConfig(endpoint)
        if getattr(transport, "client_config", None) != self.client_config:
            raise ProviderError("transport_config_mismatch")

    @property
    def resolved_model(self) -> ModelIdentity | None:
        """Return only the identity established by this instance's latest discovery."""
        return self._resolved_model

    async def discover_model(
        self, cancellation: CancellationSignal | None
    ) -> ModelIdentity:
        """Resolve the configured tag exactly once for cache identity and generation."""
        if self._resolved_model is None:
            self._resolved_model = _model_from_tags(
                await self._dispatch("GET", "/api/tags", None, cancellation),
                self._model_name,
            )
        return self._resolved_model

    async def _exchange(
        self,
        method: str,
        path: str,
        payload: JSONValue | None,
        cancellation: CancellationSignal | None,
    ) -> JSONValue:
        response = await self._transport.request(
            method,
            path,
            payload,
            client_config=self.client_config,
            cancellation=cancellation,
        )
        if type(response) is not TransportResponse:
            raise ProviderError("transport_config_mismatch")
        try:
            return await _decode_transport_response(response, self.client_config)
        finally:
            await response.aclose()

    async def _dispatch(
        self,
        method: str,
        path: str,
        payload: JSONValue | None,
        cancellation: CancellationSignal | None,
    ) -> JSONValue:
        if getattr(self._transport, "client_config", None) != self.client_config:
            raise ProviderError("transport_config_mismatch")
        try:
            if cancellation is not None and cancellation.is_set():
                raise asyncio.CancelledError
            if cancellation is None:
                return await self._exchange(method, path, payload, None)
            request_task = asyncio.create_task(
                self._exchange(method, path, payload, cancellation)
            )
            cancellation_task = asyncio.create_task(cancellation.wait())
            try:
                done, _ = await asyncio.wait(
                    (request_task, cancellation_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancellation_task in done:
                    signalled = await cancellation_task
                    if signalled or cancellation.is_set():
                        raise asyncio.CancelledError
                return await request_task
            finally:
                request_task.cancel()
                cancellation_task.cancel()
                await asyncio.gather(request_task, cancellation_task, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except ProviderError:
            raise
        except TimeoutError:
            raise ProviderError("timeout") from None
        except Exception:  # noqa: BLE001 -- transports and cancellation signals are injected.
            raise ProviderError("transport_failure") from None

    async def generate_structured(
        self,
        request: StructuredRequest,
        cancellation: CancellationSignal | None,
    ) -> ProviderResult:
        model = await self.discover_model(cancellation)
        payload: JSONValue = {
            "model": model.name,
            "messages": [
                {"role": "system", "content": request.prompt.text},
                {
                    "role": "user",
                    "content": "Evidence bundle (untrusted JSON data only):\n"
                    + request.evidence_bundle.canonical_json.decode("utf-8")
                    + (
                        "\nRepair validation error code: "
                        + request.repair_error_code
                        + ". Return one corrected response matching the unchanged schema and evidence."
                        if request.repair_error_code is not None
                        else ""
                    ),
                },
            ],
            "format": cast(JSONValue, request.response_schema.document.as_plain()),
            "stream": request.options.stream,
            "options": {"temperature": request.options.temperature, "seed": request.options.seed},
        }
        content = _chat_content(await self._dispatch("POST", "/api/chat", payload, cancellation), model)
        return ProviderResult(model, content, hashlib.sha256(content).hexdigest())
