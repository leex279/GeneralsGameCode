"""Public Task 10 package-surface checks."""

import os
import subprocess
import sys
import sysconfig
import tomllib
import zipfile
from pathlib import Path

from generals_replay_analyzer.llm import (
    AnalysisOutcome,
    AnalysisRequest,
    DeterministicFallback,
    HttpxOllamaTransport,
    OllamaAnalysisService,
)


def test_public_service_contracts_are_importable() -> None:
    assert AnalysisRequest
    assert AnalysisOutcome
    assert DeterministicFallback
    assert HttpxOllamaTransport
    assert OllamaAnalysisService


def test_runtime_declares_only_the_approved_httpx_range() -> None:
    project = Path(__file__).parents[2]
    configuration = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = configuration["project"]["dependencies"]
    assert "httpx>=0.28,<1" in dependencies
    assert not any(dependency.lower().startswith("tenacity") for dependency in dependencies)


def test_prompt_and_schema_have_exact_wheel_mappings_and_ollama_marker() -> None:
    project = Path(__file__).parents[2]
    configuration = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    force_include = configuration["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert (
        force_include["src/generals_replay_analyzer/data/strategy-report-v1.txt"]
        == "generals_replay_analyzer/data/strategy-report-v1.txt"
    )
    assert (
        force_include["src/generals_replay_analyzer/data/strategy-report-response-v1.schema.json"]
        == "generals_replay_analyzer/data/strategy-report-response-v1.schema.json"
    )
    assert any(marker.startswith("ollama:") for marker in configuration["tool"]["pytest"]["ini_options"]["markers"])


def _run(command: list[str], *, cwd: Path, environment: dict[str, str] | None = None) -> None:
    result = subprocess.run(command, cwd=cwd, env=environment, capture_output=True, text=True, check=False)
    assert result.returncode == 0, f"command failed: {command!r}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def test_installed_wheel_loads_exact_resources_and_exercises_mock_httpx_provider(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[2]
    distribution = tmp_path / "dist"
    distribution.mkdir()
    environment = os.environ.copy()
    environment["UV_CACHE_DIR"] = str(project / ".tmp" / "task10-uv-cache")
    _run(["uv", "build", "--wheel", "--out-dir", str(distribution)], cwd=project, environment=environment)
    wheel = next(distribution.glob("generals_replay_analyzer-*.whl"))
    resource_names = (
        "strategy-report-v1.txt",
        "strategy-report-response-v1.schema.json",
    )
    with zipfile.ZipFile(wheel) as archive:
        for name in resource_names:
            assert (
                archive.read(f"generals_replay_analyzer/data/{name}")
                == (project / "src" / "generals_replay_analyzer" / "data" / name).read_bytes()
            )

    isolated = tmp_path / "installed-wheel"
    _run([sys.executable, "-m", "venv", str(isolated)], cwd=tmp_path)
    python = isolated / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _run(
        [str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
        cwd=tmp_path,
    )
    proof = """
import asyncio
import hashlib
import json
import os
from pathlib import Path

import httpx

import generals_replay_analyzer
from generals_replay_analyzer.config import AnalyzerSettings
from generals_replay_analyzer.features.base import FeatureWindow
from generals_replay_analyzer.features.evidence import EvidenceRef
from generals_replay_analyzer.llm import (
    AnalysisOutcome,
    AnalysisRequest,
    DeterministicFallback,
    EvidenceClaim,
    GenerationOptions,
    HttpxOllamaTransport,
    OllamaAnalysisService,
    OllamaClientConfig,
    OllamaProvider,
    StructuredRequest,
    build_evidence_bundle,
    load_prompt,
    load_response_schema,
    validate_response,
)
from generals_replay_analyzer.storage import ContentAddressedStore
from generals_replay_analyzer.strategy.rules import RuleAssessment

async def main():
    replay_id = "00000000-0000-0000-0000-000000000001"
    evidence_id = "00000000-0000-0000-0000-000000000002"
    ref = EvidenceRef(evidence_id, "observed", "telemetry", "event:opening", "telemetry-v2")
    rule = RuleAssessment(
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
        replay_public_id=replay_id,
        replay_sha256="a" * 64,
        claims=(EvidenceClaim.from_rule_assessment(rule, authorized_evidence=(ref,)),),
    )
    response = {
        "schema_version": "strategy-report-response-v1",
        "summary": "Installed wheel response.",
        "phase_assessments": [],
        "strategy_assessments": [],
        "comparative_observations": [],
        "strengths": [],
        "vulnerabilities": [],
        "uncertainty_notes": [],
    }
    seen = []
    async def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3.6:27b", "digest": "d" * 64}]})
        return httpx.Response(200, json={"model": "qwen3.6:27b", "message": {"role": "assistant", "content": json.dumps(response, separators=(",", ":"))}, "done": True})
    config = OllamaClientConfig("http://127.0.0.1:11434")
    transport = HttpxOllamaTransport(config, transport=httpx.MockTransport(handler))
    provider = OllamaProvider(config.endpoint, "qwen3.6:27b", transport)
    result = await provider.generate_structured(
        StructuredRequest(load_prompt(), load_response_schema(), bundle, GenerationOptions()),
        None,
    )
    assert validate_response(result.response_bytes, bundle).document["summary"] == "Installed wheel response."
    settings = AnalyzerSettings(data_root=Path(os.environ["TEST_DATA_ROOT"]))
    settings.ensure_directories()
    service = OllamaAnalysisService(
        object(),
        settings=settings,
        store=ContentAddressedStore(settings.cache_directory / "llm-responses"),
        transport=transport,
    )
    assert isinstance(service, OllamaAnalysisService)
    assert all((AnalysisOutcome, AnalysisRequest, DeterministicFallback))
    assert seen == ["/api/tags", "/api/chat"]
    await transport.aclose()
    assert Path(generals_replay_analyzer.__file__).is_relative_to(Path(os.environ["TEST_ENV_ROOT"]))
    assert hashlib.sha256(load_prompt().content).hexdigest() == os.environ["TEST_PROMPT_SHA256"]
    assert hashlib.sha256(load_response_schema().content).hexdigest() == os.environ["TEST_SCHEMA_SHA256"]

asyncio.run(main())
"""
    proof_environment = os.environ.copy()
    proof_environment.pop("PYTHONPATH", None)
    proof_environment["PYTHONPATH"] = str(Path(sysconfig.get_paths()["purelib"]))
    proof_environment["TEST_ENV_ROOT"] = str(isolated)
    proof_environment["TEST_DATA_ROOT"] = str(tmp_path / "external-data")
    proof_environment["TEST_PROMPT_SHA256"] = (
        __import__("hashlib")
        .sha256((project / "src" / "generals_replay_analyzer" / "data" / resource_names[0]).read_bytes())
        .hexdigest()
    )
    proof_environment["TEST_SCHEMA_SHA256"] = (
        __import__("hashlib")
        .sha256((project / "src" / "generals_replay_analyzer" / "data" / resource_names[1]).read_bytes())
        .hexdigest()
    )
    _run([str(python), "-s", "-c", proof], cwd=tmp_path, environment=proof_environment)
