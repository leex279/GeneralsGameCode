from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

import generals_replay_analyzer.llm.service as service_module
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
    TelemetryRun,
)
from generals_replay_analyzer.features.base import FeatureWindow
from generals_replay_analyzer.features.evidence import EvidenceRef
from generals_replay_analyzer.llm.evidence_bundle import EvidenceBundle, EvidenceClaim, build_evidence_bundle
from generals_replay_analyzer.llm.provider import OllamaClientConfig, ProviderError, is_literal_loopback_endpoint
from generals_replay_analyzer.llm.service import (
    AnalysisRequest,
    DeterministicFallback,
    FallbackClaim,
    HttpxOllamaTransport,
    OllamaAnalysisService,
)
from generals_replay_analyzer.storage import ContentAddressedStore, ContentStorageError, StoredContent
from generals_replay_analyzer.strategy.rules import RuleAssessment

_LLM_IMMUTABILITY_TRIGGERS = (
    "trg_analysis_runs_succeeded_llm_no_update",
    "trg_analysis_runs_succeeded_llm_no_delete",
    "trg_managed_assets_succeeded_llm_no_update",
    "trg_managed_assets_succeeded_llm_no_delete",
    "trg_strategy_assessments_succeeded_llm_no_insert",
    "trg_strategy_assessments_succeeded_llm_no_update",
    "trg_strategy_assessments_succeeded_llm_no_delete",
    "trg_assessment_evidence_succeeded_llm_no_insert",
    "trg_assessment_evidence_succeeded_llm_no_update",
    "trg_assessment_evidence_succeeded_llm_no_delete",
    "trg_evidence_items_succeeded_llm_no_insert",
    "trg_evidence_items_succeeded_llm_no_update",
    "trg_evidence_items_succeeded_llm_no_delete",
)


def test_public_request_and_fallback_contracts_are_frozen_and_orm_free() -> None:
    with pytest.raises(TypeError, match="authority"):
        FallbackClaim(
            claim_id="opening",
            kind="rule_candidate",
            value={"strategy_id": "oil_grab"},
            quality="complete",
            quality_reason=None,
            evidence_ids=("00000000-0000-0000-0000-000000000001",),
        )
    assert not hasattr(DeterministicFallback, "_sa_instance_state")
    assert not hasattr(AnalysisRequest, "database_id")
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


@pytest.mark.anyio
@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:11434",
        "http://10.0.0.1:11434",
        "https://127.0.0.1:11434",
        "http://127.0.0.1:11434/path",
    ],
)
async def test_exported_httpx_config_and_adapter_independently_reject_nonliteral_loopback(
    endpoint: str,
) -> None:
    with pytest.raises(ValueError, match="literal loopback"):
        OllamaClientConfig(endpoint)
    valid = OllamaClientConfig("http://127.0.0.1:11434")
    forged = object.__new__(OllamaClientConfig)
    object.__setattr__(forged, "endpoint", endpoint)
    object.__setattr__(forged, "timeout", valid.timeout)
    object.__setattr__(forged, "trust_env", False)
    object.__setattr__(forged, "follow_redirects", False)
    constructed: HttpxOllamaTransport | None = None
    try:
        with pytest.raises(ValueError, match="literal loopback"):
            constructed = HttpxOllamaTransport(forged)
    finally:
        if constructed is not None:
            await constructed.aclose()


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


def _resolved_cache_identity(request: AnalysisRequest, model_digest: str) -> tuple[str, str]:
    settings_document = {
        "endpoint_policy": "loopback-http-literal-v1",
        "generation": {"seed": 0, "stream": False, "temperature": 0},
    }
    def canonical(value: object) -> bytes:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    settings_digest = hashlib.sha256(canonical(settings_document)).hexdigest()
    cache_document = {
        "cache_schema": "ollama-analysis-cache-v1",
        "provider": "ollama",
        "endpoint_policy": "loopback-http-literal-v1",
        "model_name": "qwen3.6:27b",
        "model_digest": model_digest,
        "prompt": {
            "version": "strategy-report-v1",
            "digest": "c2603f4f4cff1b3563801d83a6c2e567700eba4a86fe3af83524bcb98d6693d7",
        },
        "response_schema": {
            "version": "strategy-report-response-v1",
            "digest": "a758faf24d931094b18ade9cf37c49bbe032686f7499180872f1d7bd7669b76e",
        },
        "generation": settings_document["generation"],
        "evidence_bundle_digest": request.evidence_bundle.digest,
        "replay_player_public_id": request.replay_player_public_id,
        "settings_digest": settings_digest,
    }
    return settings_digest, hashlib.sha256(canonical(cache_document)).hexdigest()


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
async def test_nullable_player_public_identity_is_part_of_cache_and_current_request_binding(tmp_path: Path) -> None:
    settings, engine, factory, first_request, now = _seed_request(tmp_path)
    second_player_public_id = _uuid(126)
    with factory() as session:
        replay = session.scalar(select(Replay))
        parser = session.scalar(select(ParserRun))
        assert replay is not None and parser is not None
        session.add(
            ReplayPlayer(
                public_id=second_player_public_id,
                replay_id=replay.id,
                parser_run_id=parser.id,
                slot_index=1,
                slot_kind="human",
                original_name="Second",
                normalized_name="second",
                player_index=1,
                observed_json={},
            )
        )
        session.commit()
    second_request = AnalysisRequest(
        first_request.replay_public_id,
        second_player_public_id,
        first_request.evidence_bundle,
    )
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
    run_ids = iter((_uuid(127), _uuid(128)))
    service = OllamaAnalysisService(
        factory,
        settings=settings,
        store=ContentAddressedStore(settings.cache_directory / "llm-responses"),
        transport=transport,
        run_id_factory=lambda: next(run_ids),
        clock=lambda: now,
    )
    try:
        first = await service.analyze(first_request, None)
        second = await service.analyze(second_request, None)
    finally:
        await transport.aclose()
    assert (first.cache_hit, second.cache_hit) == (False, False)
    assert first.run_id != second.run_id
    assert paths == ["/api/tags", "/api/chat", "/api/tags", "/api/chat"]
    with factory() as session:
        runs = tuple(session.scalars(select(AnalysisRun).where(AnalysisRun.status == "succeeded")).all())
        assessments = tuple(
            session.scalars(select(StrategyAssessment).order_by(StrategyAssessment.analysis_run_id)).all()
        )
        assert len(runs) == 2
        assert len({run.cache_key for run in runs}) == 2
        player_by_run = {run.id: run.replay_player_id for run in runs}
        assert all(row.replay_player_id == player_by_run[row.analysis_run_id] for row in assessments)
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
@pytest.mark.parametrize(
    "tamper",
    [
        "success_diagnostics",
        "success_error",
        "success_completion",
        "inferred_parser",
        "inferred_telemetry",
        "assessment_nullable",
        "citation_tier",
        "citation_source_kind",
        "citation_source_key",
    ],
)
async def test_succeeded_cache_revalidates_exact_terminal_graph_and_current_citation_contract(
    tmp_path: Path,
    tamper: str,
) -> None:
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
    run_ids = iter((_uuid(126), _uuid(127)))
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
            for trigger_name in _LLM_IMMUTABILITY_TRIGGERS:
                session.execute(text(f"DROP TRIGGER {trigger_name}"))
            run = session.scalar(select(AnalysisRun).where(AnalysisRun.status == "succeeded"))
            inferred = session.scalar(select(EvidenceItem).where(EvidenceItem.tier == "inferred"))
            assessment = session.scalar(select(StrategyAssessment).where(StrategyAssessment.method == "llm"))
            citation = session.scalar(select(EvidenceItem).where(EvidenceItem.public_id == _uuid(102)))
            parser = session.scalar(select(ParserRun))
            assert run is not None and inferred is not None and assessment is not None
            assert citation is not None and parser is not None
            if tamper == "success_diagnostics":
                run.diagnostics_json = {"code": "poison"}
            elif tamper == "success_error":
                run.error_json = {"code": "poison"}
            elif tamper == "success_completion":
                run.completed_at = None
            elif tamper == "inferred_parser":
                inferred.parser_run_id = parser.id
            elif tamper == "inferred_telemetry":
                telemetry = TelemetryRun(
                    run_id=_uuid(128),
                    replay_id=run.replay_id,
                    schema_version=2,
                    engine_build="test-build",
                    engine_executable_sha256=None,
                    settings_json={},
                    status="running",
                    runner_status="running",
                    diagnostics_json={},
                    started_at=now,
                )
                session.add(telemetry)
                session.flush()
                inferred.telemetry_run_id = telemetry.id
            elif tamper == "assessment_nullable":
                assessment.taxonomy_version = "poison"
            elif tamper == "citation_tier":
                citation.tier = "inferred"
            elif tamper == "citation_source_kind":
                citation.source_kind = "parser"
            else:
                citation.source_key = "event:poison"
            session.commit()
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
        assert session.query(StrategyAssessment).count() == 1
        assert session.query(AnalysisRun).filter(AnalysisRun.status == "succeeded").count() == 1
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
    assert not [path for path in (settings.cache_directory / "llm-responses").rglob("*") if path.is_file()]
    engine.dispose()


def test_digest_serialization_orders_creator_rollback_before_cross_connection_reuse(tmp_path: Path) -> None:
    settings, engine, factory, request, now = _seed_request(tmp_path)
    response_bytes = json.dumps(_valid_response(_uuid(102)), separators=(",", ":")).encode()
    base_store = ContentAddressedStore(settings.cache_directory / "llm-responses")
    creator_stored = threading.Event()
    release_creator = threading.Event()
    reuser_stored = threading.Event()
    release_reuser = threading.Event()
    outcomes: dict[str, object] = {}

    class BlockingStore:
        def __init__(self, role: str) -> None:
            self.root = base_store.root
            self._role = role

        def store_bytes(self, data: bytes, *, expected_sha256: str | None = None):
            stored = base_store.store_bytes(data, expected_sha256=expected_sha256)
            if self._role == "creator":
                creator_stored.set()
                assert release_creator.wait(10)
            else:
                reuser_stored.set()
                assert release_reuser.wait(10)
            return stored

        def verify(self, sha256: str):
            return base_store.verify(sha256)

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

    class RollingBackService(OllamaAnalysisService):
        def _after_graph_insert(self, session: object) -> None:
            del session
            raise RuntimeError("creator rollback")

    def run(role: str, service_type: type[OllamaAnalysisService], run_id: str) -> None:
        async def execute() -> None:
            transport = HttpxOllamaTransport(
                OllamaClientConfig(settings.ollama_url),
                transport=httpx.MockTransport(handler),
            )
            service = service_type(
                factory,
                settings=settings,
                store=BlockingStore(role),  # type: ignore[arg-type]
                transport=transport,
                run_id_factory=lambda: run_id,
                clock=lambda: now,
            )
            try:
                outcomes[role] = await service.analyze(request, None)
            finally:
                await transport.aclose()

        asyncio.run(execute())

    creator = threading.Thread(target=run, args=("creator", RollingBackService, _uuid(132)), daemon=True)
    creator.start()
    assert creator_stored.wait(10)
    reuser = threading.Thread(target=run, args=("reuser", OllamaAnalysisService, _uuid(133)), daemon=True)
    reuser.start()
    reused_before_creator_finished = reuser_stored.wait(0.5)
    release_creator.set()
    creator.join(10)
    assert not creator.is_alive()
    assert reuser_stored.wait(10)
    release_reuser.set()
    reuser.join(10)
    assert not reuser.is_alive()
    assert reused_before_creator_finished is False
    creator_outcome = outcomes["creator"]
    reuser_outcome = outcomes["reuser"]
    assert isinstance(creator_outcome, service_module.AnalysisOutcome)
    assert isinstance(reuser_outcome, service_module.AnalysisOutcome)
    assert (creator_outcome.llm_status, creator_outcome.code) == ("failed", "persistence_failed")
    assert (reuser_outcome.llm_status, reuser_outcome.code) == ("succeeded", "ok")
    with factory() as session:
        asset = session.scalar(select(ManagedAsset))
        assert asset is not None
        assert base_store.verify(asset.sha256).path.is_file()
        assert session.query(AnalysisRun).filter(AnalysisRun.status == "succeeded").count() == 1
    engine.dispose()


def test_digest_lock_excludes_a_separate_python_process(tmp_path: Path) -> None:
    lock_path = tmp_path / "locks" / f"{'a' * 64}.lock"
    marker = tmp_path / "child-acquired"
    script = (
        "from pathlib import Path\n"
        "import sys\n"
        "from generals_replay_analyzer.llm.service import _exclusive_file_lock\n"
        "with _exclusive_file_lock(Path(sys.argv[1])):\n"
        "    Path(sys.argv[2]).write_text('acquired', encoding='utf-8')\n"
    )
    with service_module._exclusive_file_lock(lock_path):
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(lock_path), str(marker)],
            cwd=tmp_path,
        )
        assert threading.Event().wait(0.3) is False
        assert process.poll() is None
        assert not marker.exists()
    assert process.wait(timeout=10) == 0
    assert marker.read_text(encoding="utf-8") == "acquired"


def test_digest_lock_maps_filesystem_acquisition_failure_to_sanitized_storage_error(tmp_path: Path) -> None:
    occupied_parent = tmp_path / "not-a-directory"
    occupied_parent.write_bytes(b"occupied")
    with (
        pytest.raises(ContentStorageError, match="lock is unavailable"),
        service_module._exclusive_file_lock(occupied_parent / "digest.lock"),
    ):
        raise AssertionError("unreachable")


def test_digest_lock_does_not_relabel_body_io_failures(tmp_path: Path) -> None:
    with (
        pytest.raises(OSError, match="body failure"),
        service_module._exclusive_file_lock(tmp_path / "digest.lock"),
    ):
        raise OSError("body failure")


def test_service_owned_digest_boundaries_reject_invalid_or_unowned_paths(tmp_path: Path) -> None:
    settings, engine, factory, _request, _now = _seed_request(tmp_path)
    store = ContentAddressedStore(settings.cache_directory / "llm-responses")
    service = OllamaAnalysisService(
        factory,
        settings=settings,
        store=store,
        transport=object(),  # type: ignore[arg-type]
    )
    with pytest.raises(ContentStorageError, match="digest is invalid"), service._digest_guard("invalid"):
        raise AssertionError("unreachable")

    digest = "a" * 64
    outside = tmp_path / "outside"
    mismatched = store.root / "wrong" / digest
    service._cleanup_unregistered_content(StoredContent(digest, outside, 0, False))
    service._cleanup_unregistered_content(StoredContent(digest, outside, 0, True))
    service._cleanup_unregistered_content(StoredContent(digest, mismatched, 0, True))
    assert not outside.exists()
    assert not mismatched.exists()
    assert is_literal_loopback_endpoint(123) is False
    engine.dispose()


@pytest.mark.anyio
async def test_analyze_rejects_non_request_before_dispatch(tmp_path: Path) -> None:
    settings, engine, factory, _request, _now = _seed_request(tmp_path)
    service = OllamaAnalysisService(
        factory,
        settings=settings,
        store=ContentAddressedStore(settings.cache_directory / "llm-responses"),
        transport=object(),  # type: ignore[arg-type]
    )
    with pytest.raises(TypeError, match="exact AnalysisRequest"):
        await service.analyze(object(), None)  # type: ignore[arg-type]
    engine.dispose()


@pytest.mark.anyio
async def test_cleanup_query_failure_is_nonthrowing_and_leaves_created_content_fail_safe(tmp_path: Path) -> None:
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

    class QueryFailingCleanupService(OllamaAnalysisService):
        def _after_graph_insert(self, session: object) -> None:
            del session
            original_factory = self._session_factory
            calls = 0

            def fail_once():
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise SQLAlchemyError("injected registration query failure")
                return original_factory()

            self._session_factory = fail_once  # type: ignore[assignment]
            raise RuntimeError("forced rollback")

    transport = HttpxOllamaTransport(
        OllamaClientConfig(settings.ollama_url),
        transport=httpx.MockTransport(handler),
    )
    service = QueryFailingCleanupService(
        factory,
        settings=settings,
        store=ContentAddressedStore(settings.cache_directory / "llm-responses"),
        transport=transport,
        run_id_factory=lambda: _uuid(134),
        clock=lambda: now,
    )
    try:
        outcome = await service.analyze(request, None)
    finally:
        await transport.aclose()
    assert (outcome.llm_status, outcome.code) == ("failed", "persistence_failed")
    with factory() as session:
        assert session.query(ManagedAsset).count() == 0
        assert session.scalar(select(AnalysisRun)).status == "failed"  # type: ignore[union-attr]
    assert len([path for path in (settings.cache_directory / "llm-responses").rglob("*") if path.is_file()]) == 1
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
async def test_terminal_raw_collision_fails_closed_without_leaking_exception_or_rows(tmp_path: Path) -> None:
    settings, engine, factory, request, now = _seed_request(tmp_path)
    raw_responses = iter((b"{", b'{"schema_version":"wrong"}'))
    terminal_raw = b'{"schema_version":"wrong"}'
    digest = hashlib.sha256(terminal_raw).hexdigest()
    collision = settings.cache_directory / "llm-responses" / digest[:2] / digest
    collision.parent.mkdir(parents=True)
    collision.write_bytes(b"poison")

    async def handler(http_request: httpx.Request) -> httpx.Response:
        if http_request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": settings.ollama_model, "digest": "d" * 64}]})
        raw = next(raw_responses)
        return httpx.Response(
            200,
            json={
                "model": settings.ollama_model,
                "message": {"role": "assistant", "content": raw.decode()},
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
        run_id_factory=lambda: _uuid(141),
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
        assert run.raw_response_asset_id is None
        assert run.diagnostics_json["code"] == "raw_asset_collision"
        assert session.query(ManagedAsset).count() == 0
        assert session.query(StrategyAssessment).count() == 0
    engine.dispose()


@pytest.mark.anyio
async def test_terminal_transaction_failure_reconciles_new_cas_and_persists_sanitized_failure(tmp_path: Path) -> None:
    settings, engine, factory, request, now = _seed_request(tmp_path)
    raw_responses = iter(("{", "{}"))

    async def handler(http_request: httpx.Request) -> httpx.Response:
        if http_request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": settings.ollama_model, "digest": "d" * 64}]})
        return httpx.Response(
            200,
            json={
                "model": settings.ollama_model,
                "message": {"role": "assistant", "content": next(raw_responses)},
                "done": True,
            },
        )

    class FailingTerminalService(OllamaAnalysisService):
        def _after_terminal_asset(self, session: object) -> None:
            del session
            raise RuntimeError("sensitive database path")

    transport = HttpxOllamaTransport(
        OllamaClientConfig(settings.ollama_url),
        transport=httpx.MockTransport(handler),
    )
    service = FailingTerminalService(
        factory,
        settings=settings,
        store=ContentAddressedStore(settings.cache_directory / "llm-responses"),
        transport=transport,
        run_id_factory=lambda: _uuid(142),
        clock=lambda: now,
    )
    try:
        outcome = await service.analyze(request, None)
    finally:
        await transport.aclose()
    assert (outcome.llm_status, outcome.code) == ("failed", "terminal_persistence_failed")
    with factory() as session:
        run = session.scalar(select(AnalysisRun))
        assert run is not None and run.status == "failed"
        assert run.raw_response_asset_id is None
        assert run.error_json == {"code": "terminal_persistence_failed"}
        assert "path" not in json.dumps(run.diagnostics_json)
        assert session.query(ManagedAsset).count() == 0
    assert not [path for path in (settings.cache_directory / "llm-responses").rglob("*") if path.is_file()]
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
    expected_settings, expected_cache = _resolved_cache_identity(request, "d" * 64)
    with factory() as session:
        run = session.scalar(select(AnalysisRun).where(AnalysisRun.run_id == _uuid(150)))
        assert run is not None and run.status == "failed"
        assert run.diagnostics_json["code"] == "http_status_error"
        assert run.diagnostics_json["model_identity_status"] == "resolved"
        assert run.model_digest == "d" * 64
        assert (run.settings_digest, run.cache_key) == (expected_settings, expected_cache)
        assert run.raw_response_asset_id is None
        assert session.query(StrategyAssessment).count() == 0
    engine.dispose()


@pytest.mark.anyio
async def test_repair_cancellation_persists_full_resolved_cache_context(tmp_path: Path) -> None:
    settings, engine, factory, request, now = _seed_request(tmp_path)
    entered_repair = asyncio.Event()
    chat_count = 0

    async def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal chat_count
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
        entered_repair.set()
        await asyncio.Event().wait()
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
        run_id_factory=lambda: _uuid(155),
        clock=lambda: now,
    )
    task = asyncio.create_task(service.analyze(request, cancellation))
    await entered_repair.wait()
    cancellation.set()
    outcome = await task
    await transport.aclose()
    assert (outcome.llm_status, outcome.code) == ("unavailable", "cancelled")
    expected_settings, expected_cache = _resolved_cache_identity(request, "d" * 64)
    with factory() as session:
        run = session.scalar(select(AnalysisRun))
        assert run is not None
        assert run.model_digest == "d" * 64
        assert (run.settings_digest, run.cache_key) == (expected_settings, expected_cache)
        assert run.diagnostics_json["model_identity_status"] == "resolved"
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
    value = outcome.fallback.claims[0].value
    assert isinstance(value, service_module.FrozenJSONMapping)
    with pytest.raises(TypeError):
        value["strategy_id"] = "poison"  # type: ignore[index]
    nested = value["details"]
    assert isinstance(nested, service_module.FrozenJSONMapping)
    with pytest.raises(TypeError):
        nested["poison"] = True  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.fallback.claims = ()  # type: ignore[misc]
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
async def test_bundle_oversize_preflight_sends_no_http_before_materializing_fallback(tmp_path: Path) -> None:
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
    assert len(outcome.fallback.claims) == len(forged.claims) == 257
    for source, returned in zip(forged.claims, outcome.fallback.claims, strict=True):
        source_document = {
            "claim_id": source.claim_id,
            "evidence_ids": list(source.evidence_ids),
            "kind": source.kind,
            "quality": source.quality,
            "quality_reason": source.quality_reason,
            "value": service_module.thaw_canonical(source.value),
        }
        returned_document = {
            "claim_id": returned.claim_id,
            "evidence_ids": list(returned.evidence_ids),
            "kind": returned.kind,
            "quality": returned.quality,
            "quality_reason": returned.quality_reason,
            "value": (
                returned.value.as_plain()
                if isinstance(returned.value, service_module.FrozenJSONMapping)
                else returned.value
            ),
        }
        assert json.dumps(returned_document, sort_keys=True, separators=(",", ":")).encode() == json.dumps(
            source_document,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    assert calls == 0
    with factory() as session:
        run = session.scalar(select(AnalysisRun))
        assert run is not None and run.status == "unavailable"
        assert run.diagnostics_json["code"] == "evidence_bundle_oversize"
        assert session.query(StrategyAssessment).count() == 0
    engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("loader_name", "expected_code"),
    [("load_prompt", "resource_unavailable"), ("load_response_schema", "resource_digest_mismatch")],
)
async def test_missing_or_corrupt_pinned_resource_persists_sanitized_unavailable_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loader_name: str,
    expected_code: str,
) -> None:
    settings, engine, factory, request, now = _seed_request(tmp_path)
    calls = 0

    def fail_resource() -> object:
        raise service_module.ResponseValidationError(expected_code)

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    monkeypatch.setattr(service_module, loader_name, fail_resource)
    transport = HttpxOllamaTransport(
        OllamaClientConfig(settings.ollama_url),
        transport=httpx.MockTransport(handler),
    )
    service = OllamaAnalysisService(
        factory,
        settings=settings,
        store=ContentAddressedStore(settings.cache_directory / "llm-responses"),
        transport=transport,
        run_id_factory=lambda: _uuid(181),
        clock=lambda: now,
    )
    try:
        outcome = await service.analyze(request, None)
    finally:
        await transport.aclose()
    assert (outcome.llm_status, outcome.code, calls) == ("unavailable", expected_code, 0)
    assert len(outcome.fallback.claims) == 1
    with factory() as session:
        run = session.scalar(select(AnalysisRun))
        assert run is not None and run.status == "unavailable"
        assert run.prompt_digest == service_module.PROMPT_SHA256
        assert run.response_schema_digest == service_module.RESPONSE_SCHEMA_SHA256
        assert run.error_json == {"code": expected_code}
        assert expected_code in json.dumps(run.diagnostics_json)
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
