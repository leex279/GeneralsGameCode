from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from typing import cast

import pytest

from generals_replay_analyzer.features.base import FeatureWindow
from generals_replay_analyzer.features.evidence import EvidenceRef
from generals_replay_analyzer.llm.evidence_bundle import EvidenceClaim, build_evidence_bundle
from generals_replay_analyzer.llm.ollama import OllamaProvider
from generals_replay_analyzer.llm.provider import (
    GenerationOptions,
    JSONValue,
    ModelIdentity,
    OllamaClientConfig,
    ProviderError,
    ProviderResult,
    StructuredRequest,
    TransportHeaders,
    TransportResponse,
    TransportTimeout,
)
from generals_replay_analyzer.llm.schema import load_prompt, load_response_schema
from generals_replay_analyzer.strategy.rules import RuleAssessment


class FakeOllamaTransport:
    def __init__(
        self,
        responses: list[JSONValue | TransportResponse | BaseException],
        *,
        endpoint: str = "http://127.0.0.1:11434",
    ) -> None:
        self.responses = responses
        self.client_config = OllamaClientConfig(endpoint)
        self.requests: list[tuple[str, str, JSONValue | None, OllamaClientConfig, object]] = []
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None
        self.cancelled = False

    async def request(
        self,
        method: str,
        path: str,
        payload: JSONValue | None,
        *,
        client_config: OllamaClientConfig,
        cancellation: object,
    ) -> TransportResponse:
        self.requests.append((method, path, payload, client_config, cancellation))
        self.entered.set()
        try:
            if self.release is not None:
                await self.release.wait()
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            if isinstance(response, TransportResponse):
                return response
            return _raw_response(json.dumps(response, separators=(",", ":")).encode(), config=self.client_config)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def _chunks(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


def _raw_response(
    body: bytes,
    *,
    status: int = 200,
    content_type: str = "application/json",
    content_length: int | None = None,
    location: str | None = None,
    config: OllamaClientConfig | None = None,
) -> TransportResponse:
    return TransportResponse(
        status,
        TransportHeaders(
            content_type=content_type,
            content_length=len(body) if content_length is None else content_length,
            location=location,
        ),
        _chunks(body),
        config or OllamaClientConfig("http://127.0.0.1:11434"),
    )


def _request(public_ids: tuple[str, ...]) -> StructuredRequest:
    ref = EvidenceRef(public_ids[0], "observed", "telemetry", "event:opening", "telemetry-v2")
    assessment = RuleAssessment(
        strategy_id="oil_grab",
        phase="opening",
        window=FeatureWindow(0, 900),
        quality="available",
        rule_score=0.75,
        supporting_evidence=(ref,),
        contradicting_evidence=(),
        details={},
    )
    bundle = build_evidence_bundle(
        replay_public_id=public_ids[5],
        replay_sha256="c" * 64,
        claims=(EvidenceClaim.from_rule_assessment(assessment, authorized_evidence=(ref,)),),
    )
    return StructuredRequest(
        prompt=load_prompt(),
        response_schema=load_response_schema(),
        evidence_bundle=bundle,
    )


def _tags(model: str = "qwen3.6:27b", digest: str = "d" * 64) -> dict[str, JSONValue]:
    return {
        "models": [
            {
                "name": model,
                "model": model,
                "modified_at": "2026-08-22T00:00:00Z",
                "size": 123,
                "digest": digest,
                "details": {
                    "parent_model": "",
                    "format": "gguf",
                    "family": "qwen",
                    "families": ["qwen"],
                    "parameter_size": "27B",
                    "quantization_level": "Q4_K_M",
                },
            }
        ]
    }


def _chat(content: str = '{"schema_version":"strategy-report-response-v1"}') -> dict[str, JSONValue]:
    return {
        "model": "qwen3.6:27b",
        "created_at": "2026-08-22T00:00:01Z",
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": "stop",
        "total_duration": 1,
        "load_duration": 1,
        "prompt_eval_count": 1,
        "prompt_eval_duration": 1,
        "eval_count": 1,
        "eval_duration": 1,
    }


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:1", "http://127.0.0.1:65535", "http://[::1]:11434"])
def test_literal_loopback_endpoints_expose_safe_fixed_client_policy(endpoint: str) -> None:
    provider = OllamaProvider(endpoint, "qwen3.6:27b", FakeOllamaTransport([], endpoint=endpoint))
    assert provider.client_config.endpoint == endpoint
    assert provider.client_config.trust_env is False
    assert provider.client_config.follow_redirects is False
    assert provider.client_config.timeout.connect == 30.0
    assert provider.client_config.timeout.read == 30.0
    assert provider.client_config.timeout.write == 30.0
    assert provider.client_config.timeout.pool == 30.0


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:11434",
        "http://127.0.0.2:11434",
        "http://127.1:11434",
        "http://2130706433:11434",
        "http://0.0.0.0:11434",
        "http://10.0.0.1:11434",
        "https://127.0.0.1:11434",
        "http://user:pass@127.0.0.1:11434",
        "http://127.0.0.1:11434/api",
        "http://127.0.0.1:11434?proxy=x",
        "http://127.0.0.1:11434#fragment",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:011434",
        "http://[0:0:0:0:0:0:0:1]:11434",
        "http://proxy.example/?url=http://127.0.0.1:11434",
    ],
)
def test_endpoint_policy_rejects_ambiguous_or_nonliteral_urls(endpoint: str) -> None:
    with pytest.raises(ProviderError) as caught:
        OllamaProvider(endpoint, "qwen3.6:27b", FakeOllamaTransport([]))
    assert caught.value.code == "invalid_endpoint"
    assert endpoint not in repr(caught.value)


def test_transport_must_be_structurally_bound_to_exact_provider_config() -> None:
    transport = FakeOllamaTransport([], endpoint="http://127.0.0.1:11435")
    with pytest.raises(ProviderError) as caught:
        OllamaProvider("http://127.0.0.1:11434", "qwen3.6:27b", transport)
    assert caught.value.code == "transport_config_mismatch"


@pytest.mark.anyio
async def test_response_receipt_must_match_exact_provider_config(public_ids: tuple[str, ...]) -> None:
    remote = OllamaClientConfig("http://127.0.0.1:11435")
    transport = FakeOllamaTransport([_raw_response(b"{}", config=remote)])
    provider = OllamaProvider("http://127.0.0.1:11434", "qwen3.6:27b", transport)
    with pytest.raises(ProviderError) as caught:
        await provider.generate_structured(_request(public_ids), None)
    assert caught.value.code == "transport_config_mismatch"
    assert len(transport.requests) == 1


@pytest.mark.anyio
async def test_transport_config_cannot_change_after_provider_binding(public_ids: tuple[str, ...]) -> None:
    transport = FakeOllamaTransport([_tags()])
    provider = OllamaProvider("http://127.0.0.1:11434", "qwen3.6:27b", transport)
    transport.client_config = OllamaClientConfig("http://127.0.0.1:11435")
    with pytest.raises(ProviderError) as caught:
        await provider.generate_structured(_request(public_ids), None)
    assert caught.value.code == "transport_config_mismatch"
    assert transport.requests == []


@pytest.mark.anyio
async def test_headers_and_chunks_enforce_bounds_before_and_during_allocation(
    public_ids: tuple[str, ...]
) -> None:
    never_iterated = False

    async def forbidden_body() -> AsyncIterator[bytes]:
        nonlocal never_iterated
        never_iterated = True
        yield b"{}"

    config = OllamaClientConfig("http://127.0.0.1:11434")
    declared = TransportResponse(
        200,
        TransportHeaders("application/json", 262145),
        forbidden_body(),
        config,
    )
    for response, code in (
        (declared, "response_oversize"),
        (_raw_response(b"x" * 262145), "response_oversize"),
        (_raw_response(b"{}", content_length=3), "content_length_invalid"),
    ):
        transport = FakeOllamaTransport([response])
        with pytest.raises(ProviderError) as caught:
            await OllamaProvider(
                "http://127.0.0.1:11434", "qwen3.6:27b", transport
            ).generate_structured(_request(public_ids), None)
        assert caught.value.code == code
    assert never_iterated is False


@pytest.mark.anyio
async def test_invalid_or_failing_stream_chunks_are_sanitized(public_ids: tuple[str, ...]) -> None:
    async def bad_chunk() -> AsyncIterator[bytes]:
        yield cast(bytes, "not-bytes")

    async def failing_chunk() -> AsyncIterator[bytes]:
        raise RuntimeError("secret C:/private/replay.rep")
        yield b""  # pragma: no cover - keeps this an async generator

    config = OllamaClientConfig("http://127.0.0.1:11434")
    for body in (bad_chunk(), failing_chunk()):
        response = TransportResponse(
            200,
            TransportHeaders("application/json", None),
            body,
            config,
        )
        transport = FakeOllamaTransport([response])
        with pytest.raises(ProviderError) as caught:
            await OllamaProvider(
                "http://127.0.0.1:11434", "qwen3.6:27b", transport
            ).generate_structured(_request(public_ids), None)
        assert caught.value.code == "response_body_invalid"
        assert caught.value.__cause__ is None


@pytest.mark.anyio
async def test_location_header_is_rejected_even_on_success(public_ids: tuple[str, ...]) -> None:
    response = _raw_response(b"{}", location="http://remote.invalid")
    transport = FakeOllamaTransport([response])
    with pytest.raises(ProviderError) as caught:
        await OllamaProvider(
            "http://127.0.0.1:11434", "qwen3.6:27b", transport
        ).generate_structured(_request(public_ids), None)
    assert caught.value.code == "redirect_rejected"
    assert "remote" not in repr(caught.value)


@pytest.mark.parametrize(
    "construct",
    [
        lambda: TransportTimeout(read=29.0),
        lambda: OllamaClientConfig("http://127.0.0.1:1", trust_env=True),
        lambda: OllamaClientConfig("http://127.0.0.1:1", follow_redirects=True),
        lambda: TransportHeaders(cast(str, 7), 2),
        lambda: TransportHeaders("application/json", -1),
        lambda: TransportHeaders("application/json", 2, cast(str, 7)),
        lambda: _raw_response(b"{}", status=99),
        lambda: TransportResponse(
            200,
            cast(TransportHeaders, object()),
            _chunks(b"{}"),
            OllamaClientConfig("http://127.0.0.1:11434"),
        ),
        lambda: TransportResponse(
            200,
            TransportHeaders("application/json", 2),
            _chunks(b"{}"),
            cast(OllamaClientConfig, object()),
        ),
        lambda: ModelIdentity(" bad", "d" * 64),
        lambda: GenerationOptions(temperature=1),
        lambda: GenerationOptions(seed=1),
        lambda: GenerationOptions(stream=True),
        lambda: ProviderResult(ModelIdentity("qwen3.6:27b", "d" * 64), cast(bytes, "{}"), "e" * 64),
        lambda: ProviderResult(ModelIdentity("qwen3.6:27b", "d" * 64), b"{}", "E" * 64),
        lambda: ProviderResult(ModelIdentity("qwen3.6:27b", "d" * 64), b"{}", "0" * 64),
    ],
)
def test_provider_contract_values_reject_unsafe_policy(construct: object) -> None:
    with pytest.raises(ValueError):
        construct()  # type: ignore[operator]


@pytest.mark.anyio
async def test_discovery_then_chat_use_exact_model_schema_options_and_bundle(public_ids: tuple[str, ...]) -> None:
    transport = FakeOllamaTransport([_tags(), _chat("{\"ok\":true}")])
    provider = OllamaProvider("http://127.0.0.1:11434", "qwen3.6:27b", transport)
    request = _request(public_ids)
    result = await provider.generate_structured(request, None)
    assert result.model == ModelIdentity("qwen3.6:27b", "d" * 64)
    assert result.response_bytes == b'{"ok":true}'
    assert [(method, path) for method, path, *_ in transport.requests] == [
        ("GET", "/api/tags"),
        ("POST", "/api/chat"),
    ]
    assert transport.requests[0][2] is None
    payload = cast(Mapping[str, object], transport.requests[1][2])
    assert payload["model"] == "qwen3.6:27b"
    assert payload["stream"] is False
    assert payload["options"] == {"temperature": 0, "seed": 0}
    assert payload["format"] == request.response_schema.document.as_plain()
    messages = cast(list[Mapping[str, str]], payload["messages"])
    assert messages[0] == {"role": "system", "content": request.prompt.text}
    assert messages[1]["role"] == "user"
    assert request.evidence_bundle.canonical_json.decode() in messages[1]["content"]
    assert all(config == provider.client_config for *_, config, _ in transport.requests)


@pytest.mark.anyio
async def test_raw_http_json_with_utf8_content_type_is_strictly_decoded(public_ids: tuple[str, ...]) -> None:
    transport = FakeOllamaTransport(
        [
            _raw_response(json.dumps(_tags()).encode(), content_type="application/json; charset=UTF-8"),
            _raw_response(json.dumps(_chat("{\"raw\":true}")).encode()),
        ]
    )
    result = await OllamaProvider(
        "http://127.0.0.1:11434", "qwen3.6:27b", transport
    ).generate_structured(_request(public_ids), None)
    assert result.response_bytes == b'{"raw":true}'


@pytest.mark.parametrize(
    ("tags", "code"),
    [
        ({"models": []}, "model_unavailable"),
        (_tags(model="other:latest"), "model_unavailable"),
        (_tags(digest="D" * 64), "model_digest_invalid"),
        (_tags(digest="d" * 63), "model_digest_invalid"),
        ({"models": [{"name": "qwen3.6:27b", "digest": "d" * 64, "extra": "bad"}]}, "discovery_envelope_invalid"),
        ({"models": "bad"}, "discovery_envelope_invalid"),
        ({"models": [_tags()["models"][0], _tags()["models"][0]]}, "discovery_envelope_invalid"),
        ({"models": [{"name": "qwen3.6:27b", "digest": 7}]}, "discovery_envelope_invalid"),
        ({"models": [{"name": "qwen3.6:27b", "digest": "d" * 64, "size": -1}]}, "discovery_envelope_invalid"),
        (
            {"models": [{"name": "qwen3.6:27b", "digest": "d" * 64, "details": {"families": "qwen"}}]},
            "discovery_envelope_invalid",
        ),
    ],
)
@pytest.mark.anyio
async def test_discovery_failures_are_typed_and_send_no_chat(
    public_ids: tuple[str, ...], tags: JSONValue, code: str
) -> None:
    transport = FakeOllamaTransport([tags])
    provider = OllamaProvider("http://127.0.0.1:11434", "qwen3.6:27b", transport)
    with pytest.raises(ProviderError) as caught:
        await provider.generate_structured(_request(public_ids), None)
    assert caught.value.code == code
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    ("response", "code"),
    [
        ({"model": "wrong", "message": {"role": "assistant", "content": "{}"}, "done": True}, "chat_envelope_invalid"),
        ({"model": "qwen3.6:27b", "message": {"role": "tool", "content": "{}"}, "done": True}, "chat_envelope_invalid"),
        ({"model": "qwen3.6:27b", "message": {"role": "assistant", "content": "{}", "extra": 1}, "done": True}, "chat_envelope_invalid"),
        ({"model": "qwen3.6:27b", "message": {"role": "assistant", "content": "{}"}, "done": False}, "chat_envelope_invalid"),
        ({"model": "qwen3.6:27b", "done": True}, "chat_envelope_invalid"),
        ({"model": "qwen3.6:27b", "message": {"role": "assistant", "content": "{}"}, "done": True, "eval_count": -1}, "chat_envelope_invalid"),
        ({"model": "qwen3.6:27b", "message": {"role": "assistant", "content": "{}", "thinking": []}, "done": True}, "chat_envelope_invalid"),
        ({"model": "qwen3.6:27b", "message": {"role": "assistant", "content": "x" * 262145}, "done": True}, "response_oversize"),
        (_raw_response(b"{}", status=302, location="http://remote.invalid"), "redirect_rejected"),
        (_raw_response(b"{}", status=500), "http_status_error"),
        (_raw_response(b"{}", content_type="text/plain"), "content_type_invalid"),
        (_raw_response(b"\xff"), "response_utf8_invalid"),
        (_raw_response(b"{"), "response_json_invalid"),
        (_raw_response(b'{"model":"a","model":"b"}'), "response_json_invalid"),
        (_raw_response(b'{"value":NaN}'), "response_json_invalid"),
        (_raw_response(b"x" * 262145), "response_oversize"),
    ],
)
@pytest.mark.anyio
async def test_chat_adversarial_envelopes_are_typed_without_retry(
    public_ids: tuple[str, ...], response: JSONValue | TransportResponse, code: str
) -> None:
    transport = FakeOllamaTransport([_tags(), response])
    provider = OllamaProvider("http://127.0.0.1:11434", "qwen3.6:27b", transport)
    with pytest.raises(ProviderError) as caught:
        await provider.generate_structured(_request(public_ids), None)
    assert caught.value.code == code
    assert len(transport.requests) == 2
    assert "127.0.0.1" not in repr(caught.value)


@pytest.mark.anyio
async def test_model_identity_always_comes_from_discovery_and_never_accepts_a_sentinel(
    public_ids: tuple[str, ...]
) -> None:
    transport = FakeOllamaTransport([_tags(digest="e" * 64), _chat()])
    provider = OllamaProvider("http://127.0.0.1:11434", "qwen3.6:27b", transport)
    result = await provider.generate_structured(_request(public_ids), None)
    assert result.model == ModelIdentity("qwen3.6:27b", "e" * 64)
    assert [(method, path) for method, path, *_ in transport.requests] == [
        ("GET", "/api/tags"),
        ("POST", "/api/chat"),
    ]
    with pytest.raises(ValueError):
        ModelIdentity("qwen3.6:27b", "ollama-unresolved-model-digest-v1")


@pytest.mark.anyio
async def test_transport_timeout_and_failure_are_sanitized_without_retry(public_ids: tuple[str, ...]) -> None:
    for failure, code in ((TimeoutError(), "timeout"), (RuntimeError("secret /tmp/replay.rep"), "transport_failure")):
        transport = FakeOllamaTransport([failure])
        provider = OllamaProvider("http://127.0.0.1:11434", "qwen3.6:27b", transport)
        with pytest.raises(ProviderError) as caught:
            await provider.generate_structured(_request(public_ids), None)
        assert caught.value.code == code
        assert len(transport.requests) == 1
        assert "secret" not in repr(caught.value)
        assert caught.value.__cause__ is None


@pytest.mark.anyio
async def test_pre_cancelled_request_dispatches_nothing(public_ids: tuple[str, ...]) -> None:
    cancellation = asyncio.Event()
    cancellation.set()
    transport = FakeOllamaTransport([_tags()])
    provider = OllamaProvider("http://127.0.0.1:11434", "qwen3.6:27b", transport)
    with pytest.raises(asyncio.CancelledError):
        await provider.generate_structured(_request(public_ids), cancellation)
    assert transport.requests == []


@pytest.mark.anyio
async def test_in_flight_cancellation_cancels_transport_await(public_ids: tuple[str, ...]) -> None:
    cancellation = asyncio.Event()
    transport = FakeOllamaTransport([_tags()])
    transport.release = asyncio.Event()
    provider = OllamaProvider("http://127.0.0.1:11434", "qwen3.6:27b", transport)
    task = asyncio.create_task(provider.generate_structured(_request(public_ids), cancellation))
    await transport.entered.wait()
    cancellation.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.cancelled is True
    assert len(transport.requests) == 1


@pytest.mark.anyio
async def test_caller_task_cancellation_propagates_into_transport(public_ids: tuple[str, ...]) -> None:
    transport = FakeOllamaTransport([_tags()])
    transport.release = asyncio.Event()
    provider = OllamaProvider("http://127.0.0.1:11434", "qwen3.6:27b", transport)
    task = asyncio.create_task(provider.generate_structured(_request(public_ids), None))
    await transport.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.cancelled is True


@pytest.mark.anyio
async def test_exceptional_cancellation_signal_still_cancels_and_drains_transport(
    public_ids: tuple[str, ...]
) -> None:
    class ExceptionalSignal:
        def is_set(self) -> bool:
            return False

        async def wait(self) -> bool:
            raise RuntimeError("secret C:/private/replay.rep")

    transport = FakeOllamaTransport([_tags()])
    transport.release = asyncio.Event()
    provider = OllamaProvider("http://127.0.0.1:11434", "qwen3.6:27b", transport)
    with pytest.raises(ProviderError) as caught:
        await provider.generate_structured(_request(public_ids), ExceptionalSignal())
    assert caught.value.code == "transport_failure"
    assert caught.value.__cause__ is None
    assert transport.cancelled is True
