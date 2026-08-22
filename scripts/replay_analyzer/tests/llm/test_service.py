from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.db import create_database_engine, create_session_factory, upgrade_database
from generals_replay_analyzer.db.models import (
    AnalysisRun,
    AssessmentEvidence,
    EvidenceItem,
    ManagedAsset,
    ParserRun,
    Replay,
    ReplayPlayer,
    StrategyAssessment,
)
from generals_replay_analyzer.features.base import FeatureWindow
from generals_replay_analyzer.features.evidence import EvidenceRef
from generals_replay_analyzer.llm.evidence_bundle import EvidenceBundle, EvidenceClaim, build_evidence_bundle
from generals_replay_analyzer.llm.provider import OllamaClientConfig, ProviderError
from generals_replay_analyzer.llm.service import (
    AnalysisRequest,
    DeterministicFallback,
    FallbackClaim,
    HttpxOllamaTransport,
    OllamaAnalysisService,
)
from generals_replay_analyzer.storage import ContentAddressedStore
from generals_replay_analyzer.strategy.rules import RuleAssessment


def test_public_request_and_fallback_contracts_are_frozen_and_orm_free() -> None:
    claim = FallbackClaim(
        claim_id="opening",
        kind="rule_candidate",
        value={"strategy_id": "oil_grab"},
        quality="complete",
        quality_reason=None,
        evidence_ids=("00000000-0000-0000-0000-000000000001",),
    )
    fallback = DeterministicFallback(claims=(claim,))
    assert fallback.claims[0].value == {"strategy_id": "oil_grab"}
    assert not hasattr(fallback, "_sa_instance_state")
    assert not hasattr(AnalysisRequest, "database_id")
    with pytest.raises(dataclasses.FrozenInstanceError):
        fallback.claims = ()  # type: ignore[misc]
    with pytest.raises(ValueError, match="canonical UUID"):
        AnalysisRequest("not-a-uuid", None, object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="canonical UUID"):
        AnalysisRequest("AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA", None, object())  # type: ignore[arg-type]


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks
        self.close_calls = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self.close_calls += 1


@pytest.mark.anyio
async def test_httpx_transport_uses_exact_config_relative_path_and_stream_lifecycle() -> None:
    seen: list[httpx.Request] = []
    stream = _ChunkStream((b'{"models":', b"[]}"))

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "content-length": "13"},
            stream=stream,
        )

    config = OllamaClientConfig("http://127.0.0.1:11434")
    transport = HttpxOllamaTransport(config, transport=httpx.MockTransport(handler))
    response = await transport.request("GET", "/api/tags", None, client_config=config, cancellation=None)
    assert [chunk async for chunk in response.body] == [b'{"models":', b"[]}"]
    await response.aclose()
    await transport.aclose()
    assert [(request.method, request.url.path, request.url.host, request.url.port) for request in seen] == [
        ("GET", "/api/tags", "127.0.0.1", 11434)
    ]
    assert response.client_config == config
    assert response.headers.content_length == 13
    assert stream.close_calls == 1


@pytest.mark.anyio
async def test_httpx_transport_rejects_config_mismatch_before_dispatch() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    config = OllamaClientConfig("http://127.0.0.1:11434")
    transport = HttpxOllamaTransport(config, transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="transport_config_mismatch"):
        await transport.request(
            "GET",
            "/api/tags",
            None,
            client_config=OllamaClientConfig("http://127.0.0.1:11435"),
            cancellation=None,
        )
    await transport.aclose()
    assert calls == 0


def _uuid(value: int) -> str:
    return str(UUID(int=value))


def _valid_response(evidence_id: str) -> dict[str, object]:
    return {
        "schema_version": "strategy-report-response-v1",
        "summary": "Evidence-bound summary.",
        "phase_assessments": [],
        "strategy_assessments": [
            {
                "claim_id": "strategy-1",
                "strategy_label": "oil_grab",
                "phase": "opening",
                "window": {"frame_start": 0, "frame_end": 900},
                "assessment": "The deterministic candidate is supported.",
                "confidence": 0.8,
                "evidence_ids": [evidence_id],
            }
        ],
        "comparative_observations": [],
        "strengths": [],
        "vulnerabilities": [],
        "uncertainty_notes": [],
    }


def _seed_request(tmp_path: Path):
    settings = AnalyzerSettings(data_root=tmp_path / "external-task10-data")
    settings.ensure_directories()
    upgrade_database(settings.database_path)
    engine = create_database_engine(settings.database_path)
    factory = create_session_factory(engine)
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    replay_public_id = _uuid(100)
    replay_player_public_id = _uuid(101)
    evidence_public_id = _uuid(102)
    with factory() as session:
        replay = Replay(
            public_id=replay_public_id,
            sha256="a" * 64,
            replay_name="task10.rep",
            version_string="1.04",
            version_number=104,
            frame_count=900,
            start_time=1,
            end_time=2,
            exe_crc=1,
            ini_crc=2,
            map_crc=3,
            map_name="Tournament Desert",
            seed=4,
            header_json={},
            lifecycle_state="engine_verified",
            created_at=now,
            updated_at=now,
        )
        session.add(replay)
        session.flush()
        parser = ParserRun(
            run_id=_uuid(103),
            replay_id=replay.id,
            parser_version="parser-v1",
            schema_version=1,
            input_sha256=replay.sha256,
            status="running",
            warnings_json=[],
            started_at=now,
        )
        session.add(parser)
        session.flush()
        replay_player = ReplayPlayer(
            public_id=replay_player_public_id,
            replay_id=replay.id,
            parser_run_id=parser.id,
            slot_index=0,
            slot_kind="human",
            original_name="Player",
            normalized_name="player",
            player_index=0,
            observed_json={},
        )
        session.add(replay_player)
        session.flush()
        evidence = EvidenceItem(
            public_id=evidence_public_id,
            replay_id=replay.id,
            parser_run_id=parser.id,
            tier="observed",
            source_kind="telemetry",
            source_key="event:opening",
            schema_version=2,
            created_at=now,
        )
        session.add(evidence)
        session.commit()

    ref = EvidenceRef(evidence_public_id, "observed", "telemetry", "event:opening", "telemetry-v2")
    deterministic = RuleAssessment(
        strategy_id="oil_grab",
        phase="opening",
        window=FeatureWindow(0, 900),
        quality="available",
        rule_score=0.8,
        supporting_evidence=(ref,),
        contradicting_evidence=(),
        details={},
    )
    bundle = build_evidence_bundle(
        replay_public_id=replay_public_id,
        replay_sha256="a" * 64,
        claims=(EvidenceClaim.from_rule_assessment(deterministic, authorized_evidence=(ref,)),),
    )
    request = AnalysisRequest(replay_public_id, replay_player_public_id, bundle)
    return settings, engine, factory, request, now


@pytest.mark.anyio
async def test_success_persists_exact_raw_asset_and_inferred_assessment_graph(tmp_path: Path) -> None:
    settings, engine, factory, request, now = _seed_request(tmp_path)
    response_document = _valid_response(_uuid(102))
    response_bytes = json.dumps(response_document, separators=(",", ":")).encode()
    requests: list[str] = []

    async def handler(http_request: httpx.Request) -> httpx.Response:
        requests.append(http_request.url.path)
        if http_request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": settings.ollama_model, "digest": "d" * 64}]},
            )
        return httpx.Response(
            200,
            json={
                "model": settings.ollama_model,
                "message": {"role": "assistant", "content": response_bytes.decode()},
                "done": True,
            },
        )

    config = OllamaClientConfig(settings.ollama_url)
    transport = HttpxOllamaTransport(config, transport=httpx.MockTransport(handler))
    service = OllamaAnalysisService(
        factory,
        settings=settings,
        store=ContentAddressedStore(settings.cache_directory / "llm-responses"),
        transport=transport,
        run_id_factory=lambda: _uuid(110),
        clock=lambda: now,
    )
    try:
        outcome = await service.analyze(request, None)
    finally:
        await transport.aclose()

    assert outcome.llm_status == "succeeded"
    assert outcome.code == "ok"
    assert outcome.run_id == _uuid(110)
    assert outcome.cache_hit is False
    assert [(item.claim_id, item.quality, item.evidence_ids) for item in outcome.fallback.claims] == [
        ("oil_grab", "complete", (_uuid(102),))
    ]
    assert requests == ["/api/tags", "/api/chat"]
    with factory() as session:
        run = session.scalar(select(AnalysisRun))
        asset = session.scalar(select(ManagedAsset))
        inferred = session.scalar(select(EvidenceItem).where(EvidenceItem.tier == "inferred"))
        assessment = session.scalar(select(StrategyAssessment).where(StrategyAssessment.method == "llm"))
        link = session.scalar(select(AssessmentEvidence))
        cited = session.scalar(select(EvidenceItem).where(EvidenceItem.public_id == _uuid(102)))
        assert run is not None and asset is not None and inferred is not None and assessment is not None
        assert run.status == "succeeded"
        assert run.model_name == settings.ollama_model
        assert run.model_digest == "d" * 64
        assert run.input_digest == request.evidence_bundle.digest
        assert run.raw_response_asset_id == asset.id
        assert run.validated_response_json == response_document
        assert asset.kind == "analysis_raw_response"
        assert asset.media_type == "application/json"
        assert asset.size_bytes == len(response_bytes)
        assert asset.sha256 == __import__("hashlib").sha256(response_bytes).hexdigest()
        assert (settings.data_root / asset.relative_path).read_bytes() == response_bytes
        assert inferred.source_key == f"analysis-run:{_uuid(110)}:strategy-1"
        assert inferred.source_kind == "llm"
        assert assessment.analysis_run_id == run.id
        assert assessment.evidence_item_id == inferred.id
        assert assessment.method == "llm"
        assert assessment.strategy_label == "oil_grab"
        assert assessment.quality == "available"
        assert assessment.confidence == 0.8
        assert link is not None and cited is not None
        assert (link.assessment_id, link.evidence_item_id, link.role) == (
            assessment.id,
            cited.id,
            "supporting",
        )
    engine.dispose()


@pytest.mark.anyio
async def test_only_exact_succeeded_cache_is_reused_without_duplicate_graph(tmp_path: Path) -> None:
    settings, engine, factory, request, now = _seed_request(tmp_path)
    response_bytes = json.dumps(_valid_response(_uuid(102)), separators=(",", ":")).encode()
    requests: list[str] = []

    async def handler(http_request: httpx.Request) -> httpx.Response:
        requests.append(http_request.url.path)
        if http_request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": settings.ollama_model, "digest": "d" * 64}]})
        return httpx.Response(
            200,
            json={
                "model": settings.ollama_model,
                "message": {"role": "assistant", "content": response_bytes.decode()},
                "done": True,
            },
        )

    transport = HttpxOllamaTransport(
        OllamaClientConfig(settings.ollama_url),
        transport=httpx.MockTransport(handler),
    )
    run_ids = iter((_uuid(120), _uuid(121)))
    service = OllamaAnalysisService(
        factory,
        settings=settings,
        store=ContentAddressedStore(settings.cache_directory / "llm-responses"),
        transport=transport,
        run_id_factory=lambda: next(run_ids),
        clock=lambda: now,
    )
    try:
        first = await service.analyze(request, None)
        second = await service.analyze(request, None)
    finally:
        await transport.aclose()

    assert first.run_id == _uuid(120)
    assert second.run_id == first.run_id
    assert second.cache_hit is True
    assert second.validated_response == first.validated_response
    with factory() as session:
        runs = tuple(session.scalars(select(AnalysisRun).order_by(AnalysisRun.run_id)).all())
        assert [(row.run_id, row.status) for row in runs] == [
            (_uuid(120), "succeeded"),
            (_uuid(121), "unavailable"),
        ]
        assert session.query(StrategyAssessment).count() == 1
        assert session.query(EvidenceItem).filter(EvidenceItem.tier == "inferred").count() == 1
        assert session.query(ManagedAsset).count() == 1
    assert requests == ["/api/tags", "/api/chat", "/api/tags"]
    engine.dispose()


@pytest.mark.anyio
async def test_concurrent_cache_race_has_one_complete_winner_and_exact_reuse(tmp_path: Path) -> None:
    settings, engine, factory, request, now = _seed_request(tmp_path)
    response_bytes = json.dumps(_valid_response(_uuid(102)), separators=(",", ":")).encode()
    both_chats = asyncio.Event()
    chat_count = 0

    async def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal chat_count
        if http_request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": settings.ollama_model, "digest": "d" * 64}]},
            )
        chat_count += 1
        if chat_count == 2:
            both_chats.set()
        await both_chats.wait()
        return httpx.Response(
            200,
            json={
                "model": settings.ollama_model,
                "message": {"role": "assistant", "content": response_bytes.decode()},
                "done": True,
            },
        )

    transport = HttpxOllamaTransport(
        OllamaClientConfig(settings.ollama_url),
        transport=httpx.MockTransport(handler),
    )
    run_ids = iter((_uuid(122), _uuid(123)))
    service = OllamaAnalysisService(
        factory,
        settings=settings,
        store=ContentAddressedStore(settings.cache_directory / "llm-responses"),
        transport=transport,
        run_id_factory=lambda: next(run_ids),
        clock=lambda: now,
    )
    try:
        outcomes = await asyncio.gather(
            service.analyze(request, None),
            service.analyze(request, None),
        )
    finally:
        await transport.aclose()
    assert sum(not outcome.cache_hit for outcome in outcomes) == 1
    assert sum(outcome.cache_hit for outcome in outcomes) == 1
    assert {outcome.run_id for outcome in outcomes} in ({_uuid(122)}, {_uuid(123)})
    with factory() as session:
        runs = tuple(session.scalars(select(AnalysisRun)).all())
        assert sorted(run.status for run in runs) == ["succeeded", "unavailable"]
        assert session.query(ManagedAsset).count() == 1
        assert session.query(StrategyAssessment).count() == 1
        assert session.query(AssessmentEvidence).count() == 1
        assert session.query(EvidenceItem).filter(EvidenceItem.tier == "inferred").count() == 1
    engine.dispose()


@pytest.mark.anyio
async def test_corrupt_cache_is_never_reused_or_regenerated_under_same_identity(tmp_path: Path) -> None:
    settings, engine, factory, request, now = _seed_request(tmp_path)
    response_bytes = json.dumps(_valid_response(_uuid(102)), separators=(",", ":")).encode()
    paths: list[str] = []

    async def handler(http_request: httpx.Request) -> httpx.Response:
        paths.append(http_request.url.path)
        if http_request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": settings.ollama_model, "digest": "d" * 64}]},
            )
        return httpx.Response(
            200,
            json={
                "model": settings.ollama_model,
                "message": {"role": "assistant", "content": response_bytes.decode()},
                "done": True,
            },
        )

    transport = HttpxOllamaTransport(
        OllamaClientConfig(settings.ollama_url),
        transport=httpx.MockTransport(handler),
    )
    run_ids = iter((_uuid(124), _uuid(125)))
    service = OllamaAnalysisService(
        factory,
        settings=settings,
        store=ContentAddressedStore(settings.cache_directory / "llm-responses"),
        transport=transport,
        run_id_factory=lambda: next(run_ids),
        clock=lambda: now,
    )
    try:
        first = await service.analyze(request, None)
        with factory() as session:
            asset = session.scalar(select(ManagedAsset))
            assert asset is not None
            raw_path = settings.data_root / asset.relative_path
        raw_path.write_bytes(b"x" * len(response_bytes))
        second = await service.analyze(request, None)
    finally:
        await transport.aclose()
    assert (first.llm_status, first.cache_hit) == ("succeeded", False)
    assert (second.llm_status, second.code, second.cache_hit) == (
        "failed",
        "cache_validation_failed",
        False,
    )
    assert paths == ["/api/tags", "/api/chat", "/api/tags"]
    with factory() as session:
        loser = session.scalar(select(AnalysisRun).where(AnalysisRun.run_id == _uuid(125)))
        assert loser is not None and loser.status == "failed"
        assert loser.diagnostics_json["code"] == "cache_validation_failed"
        assert session.query(StrategyAssessment).count() == 1
    engine.dispose()


@pytest.mark.anyio
async def test_graph_failure_rolls_back_children_and_persists_typed_failed_attempt(tmp_path: Path) -> None:
    settings, engine, factory, request, now = _seed_request(tmp_path)
    response_bytes = json.dumps(_valid_response(_uuid(102)), separators=(",", ":")).encode()

    async def handler(http_request: httpx.Request) -> httpx.Response:
        if http_request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": settings.ollama_model, "digest": "d" * 64}]})
        return httpx.Response(
            200,
            json={
                "model": settings.ollama_model,
                "message": {"role": "assistant", "content": response_bytes.decode()},
                "done": True,
            },
        )

    class FailingService(OllamaAnalysisService):
        def _after_graph_insert(self, session: object) -> None:
            del session
            raise RuntimeError("forced rollback")

    transport = HttpxOllamaTransport(
        OllamaClientConfig(settings.ollama_url),
        transport=httpx.MockTransport(handler),
    )
    service = FailingService(
        factory,
        settings=settings,
        store=ContentAddressedStore(settings.cache_directory / "llm-responses"),
        transport=transport,
        run_id_factory=lambda: _uuid(130),
        clock=lambda: now,
    )
    try:
        outcome = await service.analyze(request, None)
    finally:
        await transport.aclose()

    assert (outcome.llm_status, outcome.code) == ("failed", "persistence_failed")
    assert outcome.fallback.claims[0].claim_id == "oil_grab"
    with factory() as session:
        run = session.scalar(select(AnalysisRun).where(AnalysisRun.run_id == _uuid(130)))
        assert run is not None and run.status == "failed"
        assert run.diagnostics_json["code"] == "persistence_failed"
        assert session.query(StrategyAssessment).count() == 0
        assert session.query(EvidenceItem).filter(EvidenceItem.tier == "inferred").count() == 0
        assert session.query(ManagedAsset).count() == 0
    engine.dispose()


@pytest.mark.anyio
async def test_raw_cas_collision_persists_failed_diagnostic_without_rows_or_asset(tmp_path: Path) -> None:
    settings, engine, factory, request, now = _seed_request(tmp_path)
    response_bytes = json.dumps(_valid_response(_uuid(102)), separators=(",", ":")).encode()
    digest = __import__("hashlib").sha256(response_bytes).hexdigest()
    collision = settings.cache_directory / "llm-responses" / digest[:2] / digest
    collision.parent.mkdir(parents=True)
    collision.write_bytes(b"not-the-response")

    async def handler(http_request: httpx.Request) -> httpx.Response:
        if http_request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": settings.ollama_model, "digest": "d" * 64}]},
            )
        return httpx.Response(
            200,
            json={
                "model": settings.ollama_model,
                "message": {"role": "assistant", "content": response_bytes.decode()},
                "done": True,
            },
        )

    transport = HttpxOllamaTransport(
        OllamaClientConfig(settings.ollama_url),
        transport=httpx.MockTransport(handler),
    )
    service = OllamaAnalysisService(
        factory,
        settings=settings,
        store=ContentAddressedStore(settings.cache_directory / "llm-responses"),
        transport=transport,
        run_id_factory=lambda: _uuid(135),
        clock=lambda: now,
    )
    try:
        outcome = await service.analyze(request, None)
    finally:
        await transport.aclose()
    assert (outcome.llm_status, outcome.code) == ("failed", "raw_asset_collision")
    with factory() as session:
        run = session.scalar(select(AnalysisRun))
        assert run is not None and run.status == "failed"
        assert run.diagnostics_json["code"] == "raw_asset_collision"
        assert run.raw_response_asset_id is None
        assert session.query(ManagedAsset).count() == 0
        assert session.query(StrategyAssessment).count() == 0
        assert session.query(EvidenceItem).filter(EvidenceItem.tier == "inferred").count() == 0
    engine.dispose()


@pytest.mark.anyio
async def test_validation_failure_gets_one_bounded_repair_then_persists_invalid(tmp_path: Path) -> None:
    settings, engine, factory, request, now = _seed_request(tmp_path)
    paths: list[str] = []
    chat_payloads: list[dict[str, object]] = []
    chat_contents = iter(("{", '{"schema_version":"wrong"}'))

    async def handler(http_request: httpx.Request) -> httpx.Response:
        paths.append(http_request.url.path)
        if http_request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": settings.ollama_model, "digest": "d" * 64}]})
        chat_payloads.append(json.loads(http_request.content))
        return httpx.Response(
            200,
            json={
                "model": settings.ollama_model,
                "message": {"role": "assistant", "content": next(chat_contents)},
                "done": True,
            },
        )

    transport = HttpxOllamaTransport(
        OllamaClientConfig(settings.ollama_url),
        transport=httpx.MockTransport(handler),
    )
    service = OllamaAnalysisService(
        factory,
        settings=settings,
        store=ContentAddressedStore(settings.cache_directory / "llm-responses"),
        transport=transport,
        run_id_factory=lambda: _uuid(140),
        clock=lambda: now,
    )
    try:
        outcome = await service.analyze(request, None)
    finally:
        await transport.aclose()

    assert (outcome.llm_status, outcome.code) == ("invalid", "response_validation_failed")
    assert outcome.fallback.claims[0].quality == "complete"
    assert paths == ["/api/tags", "/api/chat", "/api/chat"]
    first_messages = chat_payloads[0]["messages"]
    repair_messages = chat_payloads[1]["messages"]
    assert isinstance(first_messages, list) and isinstance(repair_messages, list)
    assert "Repair validation error code: invalid_json" in repair_messages[1]["content"]
    assert request.evidence_bundle.canonical_json.decode() in repair_messages[1]["content"]
    assert chat_payloads[0]["format"] == chat_payloads[1]["format"]
    assert chat_payloads[0]["options"] == chat_payloads[1]["options"] == {"temperature": 0, "seed": 0}
    with factory() as session:
        run = session.scalar(select(AnalysisRun).where(AnalysisRun.run_id == _uuid(140)))
        asset = session.scalar(select(ManagedAsset))
        assert run is not None and run.status == "invalid"
        assert run.diagnostics_json["attempts"] == [
            {"attempt": 1, "byte_count": 1, "code": "invalid_json"},
            {"attempt": 2, "byte_count": 26, "code": "response_schema_invalid"},
        ]
        assert asset is not None and run.raw_response_asset_id == asset.id
        assert (settings.data_root / asset.relative_path).read_bytes() == b'{"schema_version":"wrong"}'
        assert session.query(StrategyAssessment).count() == 0
        assert session.query(EvidenceItem).filter(EvidenceItem.tier == "inferred").count() == 0
    engine.dispose()


@pytest.mark.anyio
async def test_repair_transport_failure_has_no_third_call_and_retains_first_raw_attempt(tmp_path: Path) -> None:
    settings, engine, factory, request, now = _seed_request(tmp_path)
    paths: list[str] = []
    chat_count = 0

    async def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal chat_count
        paths.append(http_request.url.path)
        if http_request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": settings.ollama_model, "digest": "d" * 64}]},
            )
        chat_count += 1
        if chat_count == 1:
            return httpx.Response(
                200,
                json={
                    "model": settings.ollama_model,
                    "message": {"role": "assistant", "content": "{"},
                    "done": True,
                },
            )
        return httpx.Response(503, json={"error": "repair unavailable"})

    transport = HttpxOllamaTransport(
        OllamaClientConfig(settings.ollama_url),
        transport=httpx.MockTransport(handler),
    )
    service = OllamaAnalysisService(
        factory,
        settings=settings,
        store=ContentAddressedStore(settings.cache_directory / "llm-responses"),
        transport=transport,
        run_id_factory=lambda: _uuid(145),
        clock=lambda: now,
    )
    try:
        outcome = await service.analyze(request, None)
    finally:
        await transport.aclose()
    assert (outcome.llm_status, outcome.code) == ("failed", "http_status_error")
    assert paths == ["/api/tags", "/api/chat", "/api/chat"]
    with factory() as session:
        run = session.scalar(select(AnalysisRun))
        asset = session.scalar(select(ManagedAsset))
        assert run is not None and run.status == "failed"
        assert run.diagnostics_json["attempts"] == [
            {"attempt": 1, "byte_count": 1, "code": "invalid_json"}
        ]
        assert asset is not None and run.raw_response_asset_id == asset.id
        assert (settings.data_root / asset.relative_path).read_bytes() == b"{"
        assert session.query(StrategyAssessment).count() == 0
    engine.dispose()


@pytest.mark.anyio
async def test_non2xx_provider_failure_has_no_retry_and_returns_unchanged_fallback(tmp_path: Path) -> None:
    settings, engine, factory, request, now = _seed_request(tmp_path)
    paths: list[str] = []

    async def handler(http_request: httpx.Request) -> httpx.Response:
        paths.append(http_request.url.path)
        if http_request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": settings.ollama_model, "digest": "d" * 64}]})
        return httpx.Response(503, json={"error": "not available"})

    transport = HttpxOllamaTransport(
        OllamaClientConfig(settings.ollama_url),
        transport=httpx.MockTransport(handler),
    )
    service = OllamaAnalysisService(
        factory,
        settings=settings,
        store=ContentAddressedStore(settings.cache_directory / "llm-responses"),
        transport=transport,
        run_id_factory=lambda: _uuid(150),
        clock=lambda: now,
    )
    try:
        outcome = await service.analyze(request, None)
    finally:
        await transport.aclose()

    assert (outcome.llm_status, outcome.code) == ("failed", "http_status_error")
    assert paths == ["/api/tags", "/api/chat"]
    assert outcome.fallback.claims[0].evidence_ids == request.evidence_bundle.claims[0].evidence_ids
    with factory() as session:
        run = session.scalar(select(AnalysisRun).where(AnalysisRun.run_id == _uuid(150)))
        assert run is not None and run.status == "failed"
        assert run.diagnostics_json["code"] == "http_status_error"
        assert run.diagnostics_json["model_identity_status"] == "resolved"
        assert run.model_digest == "d" * 64
        assert run.raw_response_asset_id is None
        assert session.query(StrategyAssessment).count() == 0
    engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("allow_ollama", "cancelled", "expected_code"),
    [(False, False, "llm_disabled"), (True, True, "cancelled")],
)
async def test_disabled_and_pre_cancelled_requests_send_no_http_and_persist_unavailable(
    tmp_path: Path,
    allow_ollama: bool,
    cancelled: bool,
    expected_code: str,
) -> None:
    settings, engine, factory, base_request, now = _seed_request(tmp_path)
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    request = AnalysisRequest(
        base_request.replay_public_id,
        base_request.replay_player_public_id,
        base_request.evidence_bundle,
        allow_ollama=allow_ollama,
    )
    cancellation = asyncio.Event()
    if cancelled:
        cancellation.set()
    transport = HttpxOllamaTransport(
        OllamaClientConfig(settings.ollama_url),
        transport=httpx.MockTransport(handler),
    )
    service = OllamaAnalysisService(
        factory,
        settings=settings,
        store=ContentAddressedStore(settings.cache_directory / "llm-responses"),
        transport=transport,
        run_id_factory=lambda: _uuid(160 if allow_ollama else 161),
        clock=lambda: now,
    )
    try:
        outcome = await service.analyze(request, cancellation)
    finally:
        await transport.aclose()

    assert (outcome.llm_status, outcome.code) == ("unavailable", expected_code)
    assert calls == 0
    with factory() as session:
        run = session.scalar(select(AnalysisRun))
        assert run is not None and run.status == "unavailable"
        assert run.diagnostics_json["code"] == expected_code
        assert session.query(StrategyAssessment).count() == 0
    engine.dispose()


@pytest.mark.anyio
async def test_run_lifecycle_persists_pending_before_running_and_terminal_state(tmp_path: Path) -> None:
    settings, engine, factory, base_request, now = _seed_request(tmp_path)
    observed: list[str] = []

    class ObservingService(OllamaAnalysisService):
        def _after_pending(self, run_id: str) -> None:
            with factory() as session:
                run = session.scalar(select(AnalysisRun).where(AnalysisRun.run_id == run_id))
                assert run is not None
                observed.append(run.status)

    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("disabled analysis must not dispatch HTTP")

    request = AnalysisRequest(
        base_request.replay_public_id,
        base_request.replay_player_public_id,
        base_request.evidence_bundle,
        allow_ollama=False,
    )
    transport = HttpxOllamaTransport(
        OllamaClientConfig(settings.ollama_url),
        transport=httpx.MockTransport(handler),
    )
    service = ObservingService(
        factory,
        settings=settings,
        store=ContentAddressedStore(settings.cache_directory / "llm-responses"),
        transport=transport,
        run_id_factory=lambda: _uuid(165),
        clock=lambda: now,
    )
    try:
        outcome = await service.analyze(request, None)
    finally:
        await transport.aclose()
    assert observed == ["pending"]
    assert (outcome.llm_status, outcome.code) == ("unavailable", "llm_disabled")
    with factory() as session:
        run = session.scalar(select(AnalysisRun))
        assert run is not None and run.status == "unavailable"
    engine.dispose()


@pytest.mark.anyio
async def test_in_flight_cancellation_cancels_http_and_persists_closed_diagnostic(tmp_path: Path) -> None:
    settings, engine, factory, request, now = _seed_request(tmp_path)
    entered = asyncio.Event()
    transport_cancelled = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal transport_cancelled
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            transport_cancelled = True
            raise
        raise AssertionError("unreachable")

    cancellation = asyncio.Event()
    transport = HttpxOllamaTransport(
        OllamaClientConfig(settings.ollama_url),
        transport=httpx.MockTransport(handler),
    )
    service = OllamaAnalysisService(
        factory,
        settings=settings,
        store=ContentAddressedStore(settings.cache_directory / "llm-responses"),
        transport=transport,
        run_id_factory=lambda: _uuid(170),
        clock=lambda: now,
    )
    task = asyncio.create_task(service.analyze(request, cancellation))
    await entered.wait()
    cancellation.set()
    outcome = await task
    await transport.aclose()
    assert (outcome.llm_status, outcome.code) == ("unavailable", "cancelled")
    assert transport_cancelled is True
    with factory() as session:
        run = session.scalar(select(AnalysisRun))
        assert run is not None and run.status == "unavailable"
        assert run.diagnostics_json["code"] == "cancelled"
    engine.dispose()


@pytest.mark.anyio
async def test_bundle_oversize_preflight_sends_no_http_and_retains_fallback(tmp_path: Path) -> None:
    settings, engine, factory, base_request, now = _seed_request(tmp_path)
    valid = base_request.evidence_bundle
    forged = object.__new__(EvidenceBundle)
    object.__setattr__(forged, "schema_version", valid.schema_version)
    object.__setattr__(forged, "replay_public_id", valid.replay_public_id)
    object.__setattr__(forged, "replay_sha256", valid.replay_sha256)
    object.__setattr__(forged, "claims", valid.claims * 257)
    object.__setattr__(forged, "unknown_or_missing", ())
    object.__setattr__(forged, "canonical_json", b"{}")
    object.__setattr__(forged, "digest", "f" * 64)
    request = AnalysisRequest(
        base_request.replay_public_id,
        base_request.replay_player_public_id,
        forged,
    )
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    transport = HttpxOllamaTransport(
        OllamaClientConfig(settings.ollama_url),
        transport=httpx.MockTransport(handler),
    )
    service = OllamaAnalysisService(
        factory,
        settings=settings,
        store=ContentAddressedStore(settings.cache_directory / "llm-responses"),
        transport=transport,
        run_id_factory=lambda: _uuid(180),
        clock=lambda: now,
    )
    try:
        outcome = await service.analyze(request, None)
    finally:
        await transport.aclose()
    assert (outcome.llm_status, outcome.code) == ("unavailable", "evidence_bundle_oversize")
    assert len(outcome.fallback.claims) == 257
    assert calls == 0
    with factory() as session:
        run = session.scalar(select(AnalysisRun))
        assert run is not None and run.status == "unavailable"
        assert run.diagnostics_json["code"] == "evidence_bundle_oversize"
        assert session.query(StrategyAssessment).count() == 0
    engine.dispose()


@pytest.mark.ollama
@pytest.mark.anyio
async def test_opt_in_live_loopback_ollama_returns_one_validated_schema_response(tmp_path: Path) -> None:
    if os.environ.get("GENERALS_REPLAY_ANALYZER_RUN_OLLAMA_INTEGRATION") != "1":
        pytest.skip("live loopback Ollama integration is opt-in")
    settings, engine, factory, request, now = _seed_request(tmp_path)
    transport = HttpxOllamaTransport(OllamaClientConfig(settings.ollama_url))
    service = OllamaAnalysisService(
        factory,
        settings=settings,
        store=ContentAddressedStore(settings.cache_directory / "llm-responses"),
        transport=transport,
        run_id_factory=lambda: _uuid(190),
        clock=lambda: now,
    )
    try:
        outcome = await service.analyze(request, None)
    finally:
        await transport.aclose()
        engine.dispose()
    assert outcome.llm_status == "succeeded"
    assert outcome.validated_response is not None
