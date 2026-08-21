"""Authoritative determinism and non-interference evidence for replay telemetry."""

import ast
import ctypes
import hashlib
import inspect
import json
import math
import os
import re
import shutil
import stat
import textwrap
import winreg
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from generals_replay_analyzer.engine.config import EngineRunConfig
from generals_replay_analyzer.engine.outcome import ReplayOutcome, load_replay_outcome
from generals_replay_analyzer.engine.result import EngineRunStatus
from generals_replay_analyzer.engine.runner import (
    ProcessExecution,
    ProcessLaunchRequest,
    default_process_launcher,
    export_telemetry,
)
from generals_replay_analyzer.telemetry.map_asset import load_map_asset
from generals_replay_analyzer.telemetry.model import (
    CompleteRecord,
    EntitySampleRecord,
    ManifestRecord,
    MatchOutcomeRecord,
    ObjectCreatedRecord,
    PlayersInitializedRecord,
)
from generals_replay_analyzer.telemetry.reader import iter_validated_trace

_TRACE_SHA256_FIELD = re.compile(rb'("trace_sha256"\s*:\s*")[0-9a-f]{64}(")')
_ELAPSED_TIME_LINE = re.compile(
    rb"^Elapsed Time: [0-9]{2}:[0-9]{2} Game Time: [0-9]{2}:[0-9]{2}/[0-9]{2}:[0-9]{2}\r?\n?$"
)
_SHORT_ROOT_MARKER = ".task10-owner.json"
_EXPECTED_EVIDENCE = Path(__file__).parents[1] / "fixtures" / "task-10-expected-evidence.json"
_CORPUS_MANIFEST = (
    ("!Golden Replay #1.rep", 1_294_958, "cf56e1081eff70e6cfad972f5b52b096e4540cbc0c144a1becd03820d89b4d8c"),
    (
        "00-03-45_2v6_PC03_ss_HardAI_HardAI_HardAI_HardAI_HardAI_HardAI.rep",
        53_996,
        "7484beb03e790f3915ca45e8a302bfebd093edad53776bd64cdd3a707688fe9c",
    ),
    (
        "00-31-22_2v2_Derky_DESKTOPJ_HardAI_HardAI.rep",
        94_463,
        "01c7766fabb7d1dc5d3d5383ba79adf0a52942c9587509483997cf3807210688",
    ),
    (
        "00-41-30_2v2_Nic_BOMD2MAS_HardAI_HardAI.rep",
        77_245,
        "afc4de634de7d8f97baadba43f6e7db56dc7a72a3de2e0cc328854ba5980537f",
    ),
    (
        "05-01-50_2v2_amoor123_beshr_HardAI_HardAI.rep",
        46_449,
        "c587bef129122788e1c5c71fced777bfd5ca60c84058d603435beb7ab09051f8",
    ),
    (
        "11-25-57_2v2_Kana_HardAI_Erbolat_Hulk.rep",
        25_093,
        "7221f78559f9f7d96536b149ffe708902160612668614cb4c6dec0e4b74f2f73",
    ),
    (
        "12-11-35_2v2_babai_ILnur_HardAI_HardAI.rep",
        171_969,
        "e4b1d5a48f76abedb705c4a77ad4c38590bac762e6a73f6fc112dc28d9e4cc40",
    ),
    (
        "15-07-24_2v2v2_Emkill_haker_HardAI_HardAI_HardAI_HardAI.rep",
        158_710,
        "e53fda38ce6e7a3934ce608c1dcd9decc2a60d0d77055740c1eda34dbdf5d2d5",
    ),
    (
        "18-13-02_3v3_Supremac_Loonen_JB_HardAI_HardAI_HardAI.rep",
        154_620,
        "be3a803aba44449ee0be5d841d51d0775370ffecea81418cd58f625201f81d7f",
    ),
    ("366648.rep", 72_613, "19537ab5e4021e0306bde18badab1f6788eadbb5270aef0114831933ffec89f8"),
)
_CUSTOM_MAP_MANIFEST = (
    (
        "[RANK] Arctic Arena ZH v1/[RANK] Arctic Arena ZH v1.map",
        162_490,
        "2eb85558096903966136455360519e0a471cc92969c16d64183f37a6eb905235",
    ),
    (
        "[RANK] Arctic Arena ZH v1/[RANK] Arctic Arena ZH v1.tga",
        65_580,
        "3d0c4847f50d6657eb5196d76e6f514bfdad9f7ea645b9517a2a48f430be2b11",
    ),
    (
        "[RANK] Arctic Arena ZH v1/map.ini",
        1_755,
        "2495397fff9533f6bd18ba18a4d2a6dbcd706fe88516cf46dd31136815b3e335",
    ),
    (
        "[RANK] Arctic Arena ZH v1/map.str",
        65,
        "9408d9dc226a16b651733592a5ed92249691ddf510c9dc920293c19dcaf3f44c",
    ),
    ("tansooo/tansooo.map", 106_110, "39da380ce94f2c4a23d58448ac4f7c14117312e9c057fb5679856aed452c3141"),
    ("tansooo/tansooo.tga", 65_580, "8f0a9e059e482eefe0cea773e97942766bb5765ee31931f9cbb99807f962284f"),
)
_PINNED_MAP_MANIFEST = (
    (
        "[RANK] Sand Scorpion.map",
        164_094,
        "19de5251fcc5c0d43532e462281b13c3ece302e8c4e9e360bc3b6ea59431964e",
    ),
    (
        "[RANK] Sand Scorpion.tga",
        65_580,
        "9ea420b954e88227138410155b8665be261337395254bbd7966646ac8668b8b7",
    ),
    ("map.str", 129, "c32d98b971fb02410532caaf21fc988aa55af617a0f6ceaaeb366b93ca66a8be"),
)


@dataclass(frozen=True)
class MapEvidence:
    schema_version: int
    content_sha256: str
    manifest_sha256: str
    file_sha256: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class BridgeBindingEvidence:
    bridge_index: int
    object_id: int
    template_name: str
    initialization_snapshot_status: str
    orientation: float
    orientation_from_map_endpoints: float


@dataclass(frozen=True)
class WaypointEdgeEvidence:
    source_waypoint_id: int
    source_name: str
    target_waypoint_id: int
    target_name: str


@dataclass(frozen=True)
class OobExemptionEvidence:
    object_id: int
    template_name: str
    position: tuple[float, float, float]


@dataclass(frozen=True)
class RuntimeMapFacts:
    bridge_bindings: tuple[BridgeBindingEvidence, ...]
    duplicate_label_edges: tuple[WaypointEdgeEvidence, ...]
    oob_exemptions: tuple[OobExemptionEvidence, ...]


@dataclass(frozen=True)
class RunEvidence:
    exit_code: int
    outcome: ReplayOutcome
    stdout: bytes
    stderr: bytes
    normalized_trace_sha256: str | None
    map_evidence: MapEvidence | None
    match_outcome: dict[str, Any] | None
    player_names: tuple[str, ...]
    runtime_map_facts: RuntimeMapFacts | None


def _json_string(value: str) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _replace_json_field(source: bytes, field: str, original: str, replacement: bytes) -> bytes:
    prefix = rb'("' + re.escape(field.encode("ascii")) + rb'"\s*:\s*)'
    pattern = re.compile(prefix + re.escape(_json_string(original)))
    return pattern.sub(lambda match: match.group(1) + replacement, source)


def _normalize_trace_bytes(
    source: bytes,
    *,
    run_id: str,
    run_paths: tuple[str, ...] = (),
    wall_clock_timestamps: tuple[str, ...] = (),
) -> bytes:
    """Replace only explicitly supplied run-local scalars without parsing or reserializing records."""
    lines = source.splitlines(keepends=True)
    if not lines or not lines[-1].endswith(b"\n"):
        raise ValueError("trace must contain newline-terminated records")

    normalized_lines: list[bytes] = []
    for line in lines:
        line = _replace_json_field(line, "run_id", run_id, b'"<run-id>"')
        for path in run_paths:
            line = _replace_json_field(line, "path", path, b'"<run-path>"')
        for timestamp in wall_clock_timestamps:
            line = _replace_json_field(line, "created_at", timestamp, b'"<wall-clock>"')
        normalized_lines.append(line)

    normalized_prior = b"".join(normalized_lines[:-1])
    normalized_digest = hashlib.sha256(normalized_prior).hexdigest().encode("ascii")
    normalized_complete, count = _TRACE_SHA256_FIELD.subn(
        rb"\g<1>" + normalized_digest + rb"\g<2>", normalized_lines[-1]
    )
    if count != 1:
        raise ValueError("terminal completion record must contain exactly one trace_sha256")
    return normalized_prior + normalized_complete


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _deterministic_stdout(source: bytes) -> bytes:
    """Remove only the engine's explicitly wall-clock-dependent elapsed-time lines."""
    return b"".join(line for line in source.splitlines(keepends=True) if _ELAPSED_TIME_LINE.fullmatch(line) is None)


def _terminal_facts(outcome: ReplayOutcome) -> tuple[bool, int, int, str, bool, int | None]:
    return (
        outcome.playback_started,
        outcome.final_frame,
        outcome.command_count,
        outcome.terminal_reason,
        outcome.crc_mismatch,
        outcome.crc_mismatch_frame,
    )


def _terminal_evidence(run: RunEvidence) -> dict[str, object]:
    return {
        "exit_code": run.exit_code,
        "playback_started": run.outcome.playback_started,
        "final_frame": run.outcome.final_frame,
        "command_count": run.outcome.command_count,
        "terminal_reason": run.outcome.terminal_reason,
        "crc_mismatch": run.outcome.crc_mismatch,
        "crc_mismatch_frame": run.outcome.crc_mismatch_frame,
    }


def _enabled_evidence(run: RunEvidence) -> dict[str, object]:
    assert run.map_evidence is not None
    return {
        "normalized_trace_sha256": run.normalized_trace_sha256,
        "map": {
            "schema_version": run.map_evidence.schema_version,
            "content_sha256": run.map_evidence.content_sha256,
            "manifest_sha256": run.map_evidence.manifest_sha256,
            "file_sha256": dict(run.map_evidence.file_sha256),
        },
        "match_outcome": run.match_outcome,
    }


def _expected_evidence() -> dict[str, Any]:
    return json.loads(_EXPECTED_EVIDENCE.read_text(encoding="utf-8"))


def _map_evidence(paths: tuple[Path, ...]) -> MapEvidence:
    by_name = {path.name: path for path in paths}
    if set(by_name) != {
        "height.f32.zlib",
        "manifest.json",
        "pathing-amphibious.u8.zlib",
        "pathing-ground.u8.zlib",
        "terrain.u8.zlib",
        "zones.i32.zlib",
    }:
        raise AssertionError(f"incomplete map asset output: {sorted(by_name)}")
    manifest_path = by_name["manifest.json"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return MapEvidence(
        schema_version=manifest["schema_version"],
        content_sha256=manifest["content_sha256"],
        manifest_sha256=_sha256_file(manifest_path),
        file_sha256=tuple((name, _sha256_file(by_name[name])) for name in sorted(by_name)),
    )


def _read_log(path: Path) -> bytes:
    return path.read_bytes()


def _run_disabled(
    executable: Path,
    replay: Path,
    run_dir: Path,
    *,
    run_id: str,
    replay_user_data_root: Path | None = None,
    timeout_seconds: int = 120,
) -> RunEvidence:
    """Run only the independent outcome side channel through Task 9's public launcher seam."""
    run_dir.mkdir(parents=True)
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    outcome_path = run_dir / "replay-outcome.json"
    argv_parts = [
        str(executable),
        "-headless",
        "-noaudio",
        "-replay",
        str(replay),
        "-replay-outcome",
        str(outcome_path),
    ]
    if replay_user_data_root is not None:
        argv_parts.extend(("-replay-user-data-root", str(replay_user_data_root)))
    argv = tuple(argv_parts)
    assert "-telemetry" not in argv and "-jobs" not in argv
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        execution = default_process_launcher(
            ProcessLaunchRequest(
                run_id=run_id,
                run_dir=run_dir,
                argv=argv,
                cwd=executable.parent,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                stdout_handle=stdout,
                stderr_handle=stderr,
                timeout_seconds=timeout_seconds,
            )
        )
    assert isinstance(execution, ProcessExecution)
    assert not execution.timed_out
    assert outcome_path.is_file(), f"disabled run did not publish an outcome; stderr={_read_log(stderr_path)!r}"
    return RunEvidence(
        exit_code=execution.exit_code,
        outcome=load_replay_outcome(outcome_path),
        stdout=_deterministic_stdout(_read_log(stdout_path)),
        stderr=_read_log(stderr_path),
        normalized_trace_sha256=None,
        map_evidence=None,
        match_outcome=None,
        player_names=(),
        runtime_map_facts=None,
    )


def _runtime_map_facts(
    trace_path: Path,
    records: tuple[Any, ...],
    manifest: ManifestRecord,
) -> RuntimeMapFacts:
    reference = manifest.payload.map_asset
    assert reference is not None
    asset = load_map_asset(
        trace_path.parent / Path(*reference.path.split("/")),
        expected_reference=reference,
        expected_engine_data_identity=manifest.payload.engine_build,
        expected_map_identity=manifest.payload.map_identity,
        trusted_trace_directory=trace_path.parent,
    )
    creations = {
        record.payload.object_id: record.payload
        for record in records
        if isinstance(record, ObjectCreatedRecord)
    }
    bridge_bindings: list[BridgeBindingEvidence] = []
    for bridge in asset.bridges:
        if bridge.object_id is None or bridge.template_name is None:
            continue
        creation = creations[bridge.object_id]
        assert creation.creation_source == "map_loaded"
        assert creation.template_name == bridge.template_name
        assert creation.initialization_snapshot_status is not None
        bridge_bindings.append(
            BridgeBindingEvidence(
                bridge_index=bridge.bridge_index,
                object_id=bridge.object_id,
                template_name=bridge.template_name,
                initialization_snapshot_status=creation.initialization_snapshot_status,
                orientation=creation.orientation,
                orientation_from_map_endpoints=math.atan2(
                    bridge.to.y - bridge.from_.y,
                    bridge.to.x - bridge.from_.x,
                ),
            )
        )

    name_counts: dict[str, int] = {}
    waypoints_by_id = {waypoint.waypoint_id: waypoint for waypoint in asset.waypoints}
    for waypoint in asset.waypoints:
        name_counts[waypoint.name] = name_counts.get(waypoint.name, 0) + 1
    duplicate_label_edges: list[WaypointEdgeEvidence] = []
    for waypoint in asset.waypoints:
        for target_id in waypoint.link_waypoint_ids or ():
            target = waypoints_by_id[target_id]
            if name_counts[target.name] > 1:
                duplicate_label_edges.append(
                    WaypointEdgeEvidence(
                        source_waypoint_id=waypoint.waypoint_id,
                        source_name=waypoint.name,
                        target_waypoint_id=target_id,
                        target_name=target.name,
                    )
                )

    oob_exemptions = {
        OobExemptionEvidence(
            object_id=record.payload.object_id,
            template_name=record.payload.template_name or "",
            position=(record.payload.position.x, record.payload.position.y, record.payload.position.z),
        )
        for record in records
        if isinstance(record, EntitySampleRecord)
        and record.payload.position_bounds_policy == "exempt_map_loaded_unclassified_immobile"
        and (
            record.payload.position.x < asset.bounds.minimum.x
            or record.payload.position.x > asset.bounds.maximum.x
            or record.payload.position.y < asset.bounds.minimum.y
            or record.payload.position.y > asset.bounds.maximum.y
        )
    }
    return RuntimeMapFacts(
        bridge_bindings=tuple(sorted(bridge_bindings, key=lambda item: item.bridge_index)),
        duplicate_label_edges=tuple(
            sorted(duplicate_label_edges, key=lambda item: (item.source_waypoint_id, item.target_waypoint_id))
        ),
        oob_exemptions=tuple(sorted(oob_exemptions, key=lambda item: item.object_id)),
    )


def _run_enabled(
    executable: Path,
    replay: Path,
    data_root: Path,
    *,
    run_id: str,
    replay_user_data_root: Path | None = None,
    timeout_seconds: int = 120,
) -> RunEvidence:
    result = export_telemetry(
        replay,
        EngineRunConfig(
            executable=executable,
            timeout_seconds=timeout_seconds,
            movement_sample_frames=15,
            data_root=data_root,
            replay_user_data_root=replay_user_data_root,
        ),
        run_id_factory=lambda: run_id,
    )
    diagnostic_stderr = (
        result.stderr_path.read_bytes() if result.stderr_path is not None and result.stderr_path.is_file() else b""
    )
    diagnostic_outcome = (
        result.outcome_path.read_bytes() if result.outcome_path is not None and result.outcome_path.is_file() else b""
    )
    if result.status not in {EngineRunStatus.SUCCESS, EngineRunStatus.VALID_CRC_MISMATCH}:
        raise AssertionError(
            f"enabled run status={result.status.value}; "
            f"diagnostics={[(item.code, item.message) for item in result.diagnostics]!r}; "
            f"stderr={diagnostic_stderr.decode('utf-8', errors='replace')!r}; "
            f"outcome={diagnostic_outcome.decode('utf-8', errors='replace')!r}"
        )
    assert result.exit_code is not None
    assert result.trace_path is not None
    assert result.outcome_path is not None
    assert result.stdout_path is not None
    assert result.stderr_path is not None
    records = tuple(iter_validated_trace(result.trace_path))
    assert isinstance(records[0], ManifestRecord)
    assert isinstance(records[-1], CompleteRecord)
    assert [record.sequence for record in records] == list(range(len(records)))
    complete = records[-1]
    assert complete.payload.writer_error is None
    players = next(record for record in records if isinstance(record, PlayersInitializedRecord))
    player_names = tuple(
        slot.replay_name for slot in (players.payload.slots or ()) if slot.replay_name is not None
    )
    match = next(record for record in records if isinstance(record, MatchOutcomeRecord))
    normalized = _normalize_trace_bytes(result.trace_path.read_bytes(), run_id=run_id)
    return RunEvidence(
        exit_code=result.exit_code,
        outcome=load_replay_outcome(result.outcome_path),
        stdout=_deterministic_stdout(_read_log(result.stdout_path)),
        stderr=_read_log(result.stderr_path),
        normalized_trace_sha256=hashlib.sha256(normalized).hexdigest(),
        map_evidence=_map_evidence(result.map_assets),
        match_outcome=match.payload.model_dump(mode="json"),
        player_names=player_names,
        runtime_map_facts=_runtime_map_facts(result.trace_path, records, records[0]),
    )


@contextmanager
def _short_root(tmp_path: Path, label: str) -> Iterator[Path]:
    """Create and remove only an identity-marked short root owned by this test invocation."""
    token = hashlib.sha256(f"{tmp_path}-{label}-{uuid4()}".encode()).hexdigest()[:10]
    parent = tmp_path.parents[1].resolve()
    root = (parent / f"g10-{token}").resolve()
    if root.parent != parent or root.name != f"g10-{token}":
        raise AssertionError(f"unsafe short output root: {root}")
    root.mkdir(exist_ok=False)
    marker = root / _SHORT_ROOT_MARKER
    identity = {"root": str(root), "token": token}
    marker.write_text(json.dumps(identity, sort_keys=True), encoding="utf-8")
    try:
        yield root
    finally:
        if not marker.is_file() or marker.is_symlink():
            raise AssertionError(f"refusing cleanup without a regular identity marker: {root}")
        if json.loads(marker.read_text(encoding="utf-8")) != identity:
            raise AssertionError(f"refusing cleanup after output-root identity changed: {root}")
        if root.resolve() != root or root.parent != parent or root.name != f"g10-{token}":
            raise AssertionError(f"refusing cleanup of unexpected output root: {root}")
        shutil.rmtree(root)


def _default_user_data_root() -> Path:
    """Resolve the redirected Documents folder and registry leaf used by the x86 Zero Hour engine."""
    documents = ctypes.create_unicode_buffer(32_768)
    result = ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, documents)
    assert result == 0 and documents.value
    registry_path = r"SOFTWARE\Electronic Arts\EA Games\Command and Conquer Generals Zero Hour"
    leaf_name: str | None = None
    access_modes = (winreg.KEY_READ | winreg.KEY_WOW64_32KEY, winreg.KEY_READ)
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for access in access_modes:
            try:
                with winreg.OpenKey(hive, registry_path, 0, access) as key:
                    value, _kind = winreg.QueryValueEx(key, "UserDataLeafName")
            except OSError:
                continue
            if isinstance(value, str) and value:
                leaf_name = value
                break
        if leaf_name is not None:
            break
    if leaf_name is None:
        leaf_name = "Command and Conquer Generals Zero Hour Data"
    return (Path(documents.value) / leaf_name).resolve()


def _user_data_inventory(data_root: Path | None = None) -> tuple[tuple[str, str, int, int, str | None], ...]:
    """Fail closed while inventorying the actual user-data root without following reparse entries."""
    if data_root is None:
        data_root = _default_user_data_root()
    entries: list[tuple[str, str, int, int, str | None]] = []
    try:
        root_metadata = os.lstat(data_root)
    except FileNotFoundError:
        return ((".", "absent", 0, 0, None),)

    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

    def visit(path: Path, relative: str, metadata: os.stat_result) -> None:
        is_reparse = bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)
        if is_reparse:
            kind = "reparse"
            sha256 = None
        elif stat.S_ISLNK(metadata.st_mode):
            kind = "symlink"
            sha256 = None
        elif stat.S_ISREG(metadata.st_mode):
            kind = "regular_file"
            sha256 = _sha256_file(path)
        elif stat.S_ISDIR(metadata.st_mode):
            kind = "directory"
            sha256 = None
        else:
            kind = "other"
            sha256 = None
        entries.append((relative, kind, metadata.st_size, metadata.st_mtime_ns, sha256))

        if kind != "directory":
            return
        with os.scandir(path) as iterator:
            children = sorted(iterator, key=lambda entry: entry.name)
        for child in children:
            child_path = Path(child.path)
            child_relative = child.name if relative == "." else f"{relative}/{child.name}"
            visit(child_path, child_relative, os.lstat(child_path))

    visit(data_root, ".", root_metadata)
    return tuple(sorted(entries))


def _retail_corpus(replay_root: Path) -> tuple[Path, ...]:
    """Return the exact documented corpus only after validating name, size, and bytes."""
    replays = tuple(sorted(replay_root.glob("*.rep")))
    actual = tuple((replay.name, replay.stat().st_size, _sha256_file(replay)) for replay in replays)
    assert actual == _CORPUS_MANIFEST
    return replays


def _relative_file_manifest(root: Path) -> tuple[tuple[str, int, str], ...]:
    """Hash only ordinary non-reparse files below one pinned source or isolated destination."""
    entries: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        is_reparse = bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        assert not path.is_symlink() and not is_reparse
        if stat.S_ISREG(info.st_mode):
            entries.append((path.relative_to(root).as_posix(), info.st_size, _sha256_file(path)))
        else:
            assert stat.S_ISDIR(info.st_mode)
    return tuple(entries)


def _stage_isolated_user_data(repository_root: Path, destination: Path) -> Path:
    """Copy the exact pinned custom-map corpus into one never-reused test-owned user-data root."""
    source = repository_root / "GeneralsReplays" / "GeneralsZH" / "1.04" / "Maps"
    assert _relative_file_manifest(source) == _CUSTOM_MAP_MANIFEST
    destination.mkdir()
    maps = destination / "Maps"
    maps.mkdir()
    for relative, _size, _sha256 in _CUSTOM_MAP_MANIFEST:
        source_path = source / Path(*relative.split("/"))
        destination_path = maps / Path(*relative.split("/"))
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination_path)
    assert _relative_file_manifest(maps) == _CUSTOM_MAP_MANIFEST
    return destination


def _stage_pinned_replay_user_data(destination: Path) -> Path:
    """Copy the exact preexisting pinned-fixture map into a fresh root without writing to its source profile."""
    source = _default_user_data_root() / "Maps" / "[RANK] Sand Scorpion"
    assert _relative_file_manifest(source) == _PINNED_MAP_MANIFEST
    destination.mkdir()
    target = destination / "Maps" / source.name
    target.mkdir(parents=True)
    for relative, _size, _sha256 in _PINNED_MAP_MANIFEST:
        shutil.copyfile(source / relative, target / relative)
    assert _relative_file_manifest(target) == _PINNED_MAP_MANIFEST
    return destination


def _summary(replay: Path, mode: str, evidence: RunEvidence) -> dict[str, object]:
    return {
        "replay": replay.name,
        "mode": mode,
        "exit_code": evidence.exit_code,
        **dict(zip(
            ("playback_started", "final_frame", "command_count", "terminal_reason", "crc_mismatch", "crc_frame"),
            _terminal_facts(evidence.outcome),
            strict=True,
        )),
        "normalized_trace_sha256": evidence.normalized_trace_sha256,
        "map_schema_version": evidence.map_evidence.schema_version if evidence.map_evidence else None,
        "map_content_sha256": evidence.map_evidence.content_sha256 if evidence.map_evidence else None,
        "manifest_sha256": evidence.map_evidence.manifest_sha256 if evidence.map_evidence else None,
        "map_file_sha256": dict(evidence.map_evidence.file_sha256) if evidence.map_evidence else None,
        "match_outcome": evidence.match_outcome,
    }


def test_normalization_changes_only_run_specific_fields_and_rebinds_completion_digest() -> None:
    """Catch normalization that hides simulation ordering, values, numeric spellings, or float bits."""
    run_id = "11111111-1111-4111-8111-111111111111"
    run_path = r"C:\short\runs\11111111-1111-4111-8111-111111111111\trace.ndjson"
    first = (
        b'{"schema_version":2,"run_id":"11111111-1111-4111-8111-111111111111",'
        b'"sequence":0,"frame":7,"logic_time_seconds":0.23333333333333334,'
        b'"event_type":"manifest","payload":{"created_at":"2026-08-21T12:34:56.123456Z",'
        b'"path":"C:\\\\short\\\\runs\\\\11111111-1111-4111-8111-111111111111\\\\trace.ndjson",'
        b'"value":1.25000000}}\n'
    )
    original_digest = hashlib.sha256(first).hexdigest()
    complete = (
        '{"schema_version":2,"run_id":"11111111-1111-4111-8111-111111111111",'
        '"sequence":1,"frame":7,"logic_time_seconds":0.23333333333333334,'
        '"event_type":"complete","payload":{"trace_sha256":"'
        + original_digest
        + '"}}\n'
    ).encode()

    normalized = _normalize_trace_bytes(
        first + complete,
        run_id=run_id,
        run_paths=(run_path,),
        wall_clock_timestamps=("2026-08-21T12:34:56.123456Z",),
    )

    assert b'"run_id":"<run-id>"' in normalized
    assert b'"created_at":"<wall-clock>"' in normalized
    assert b'"path":"<run-path>"' in normalized
    assert b'"sequence":0,"frame":7,"logic_time_seconds":0.23333333333333334' in normalized
    assert b'"value":1.25000000' in normalized
    normalized_prior = normalized.split(b'\n', maxsplit=1)[0] + b'\n'
    assert f'"trace_sha256":"{hashlib.sha256(normalized_prior).hexdigest()}"'.encode() in normalized


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (b'"sequence":0', b'"sequence":1'),
        (b'"frame":7', b'"frame":8'),
        (b'"logic_time_seconds":0.23333333333333334', b'"logic_time_seconds":0.23333333333333335'),
        (b'"value":1.25000000', b'"value":1.25'),
        (b'"event_type":"manifest"', b'"event_type":"changed"'),
        (b'"sequence":0,"frame":7', b'"frame":7,"sequence":0'),
    ],
)
def test_normalization_detects_every_non_run_mutation(old: bytes, new: bytes) -> None:
    """Catch a broad normalizer that hides semantic, ordering, spelling, or float-bit changes."""
    run_id = "11111111-1111-4111-8111-111111111111"
    first = (
        b'{"run_id":"11111111-1111-4111-8111-111111111111","sequence":0,"frame":7,'
        b'"logic_time_seconds":0.23333333333333334,"event_type":"manifest",'
        b'"payload":{"value":1.25000000}}\n'
    )
    complete = (
        b'{"run_id":"11111111-1111-4111-8111-111111111111","sequence":1,"frame":7,'
        b'"logic_time_seconds":0.23333333333333334,"event_type":"complete",'
        b'"payload":{"trace_sha256":"' + hashlib.sha256(first).hexdigest().encode() + b'"}}\n'
    )
    source = first + complete
    mutated = source.replace(old, new, 1)

    assert mutated != source
    assert _normalize_trace_bytes(mutated, run_id=run_id) != _normalize_trace_bytes(source, run_id=run_id)


def test_stdout_normalization_removes_only_elapsed_time_lines() -> None:
    """Catch stdout filtering that conceals any deterministic engine diagnostic."""
    source = (
        b"start\r\n"
        b"Elapsed Time: 00:01 Game Time: 00:03/00:03\r\n"
        b"prefix Elapsed Time: 00:01 Game Time: 00:03/00:03\r\n"
        b"Elapsed Time: changed diagnostic\r\n"
        b"CRC Mismatch in Frame 105\r\n"
    )
    assert _deterministic_stdout(source) == (
        b"start\r\n"
        b"prefix Elapsed Time: 00:01 Game Time: 00:03/00:03\r\n"
        b"Elapsed Time: changed diagnostic\r\n"
        b"CRC Mismatch in Frame 105\r\n"
    )
    assert _deterministic_stdout(source.replace(b"105", b"106")) != _deterministic_stdout(source)


def test_user_data_inventory_records_file_kind_and_sha256(tmp_path: Path) -> None:
    """Catch inventories that could miss same-size content changes or unsafe non-files."""
    data_root = tmp_path / "profile"
    custom_map = data_root / "Maps" / "custom" / "custom.map"
    replay = data_root / "Replays" / "fixture.rep"
    custom_map.parent.mkdir(parents=True)
    replay.parent.mkdir(parents=True)
    custom_map.write_bytes(b"map-bytes")
    replay.write_bytes(b"replay-bytes")

    inventory = {entry[0]: entry[1:] for entry in _user_data_inventory(data_root)}

    assert inventory["Maps/custom"][0] == "directory"
    assert isinstance(inventory["Maps/custom"][2], int)
    assert inventory["Maps/custom"][3] is None
    assert inventory["Maps/custom/custom.map"][:2] == ("regular_file", len(b"map-bytes"))
    assert isinstance(inventory["Maps/custom/custom.map"][2], int)
    assert inventory["Maps/custom/custom.map"][3] == hashlib.sha256(b"map-bytes").hexdigest()
    assert inventory["Replays/fixture.rep"][:2] == ("regular_file", len(b"replay-bytes"))
    assert isinstance(inventory["Replays/fixture.rep"][2], int)
    assert inventory["Replays/fixture.rep"][3] == hashlib.sha256(b"replay-bytes").hexdigest()


def test_user_data_inventory_propagates_scan_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Catch recursive APIs that silently suppress an unreadable real user-data subtree."""
    data_root = tmp_path / "profile"
    data_root.mkdir()
    original_scandir = os.scandir

    def denied_scandir(path: os.PathLike[str] | str) -> Any:
        if Path(path) == data_root:
            raise PermissionError("inventory scan denied")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", denied_scandir)

    with pytest.raises(PermissionError, match="inventory scan denied"):
        _user_data_inventory(data_root)


def test_user_data_inventory_propagates_nested_stat_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch a disappearing or unreadable entry being omitted from the postcondition snapshot."""
    data_root = tmp_path / "profile"
    blocked = data_root / "blocked"
    data_root.mkdir()
    blocked.write_bytes(b"present")
    original_lstat = os.lstat

    def denied_lstat(path: os.PathLike[str] | str, *args: object, **kwargs: object) -> os.stat_result:
        if Path(path) == blocked:
            raise PermissionError("inventory stat denied")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", denied_lstat)

    with pytest.raises(PermissionError, match="inventory stat denied"):
        _user_data_inventory(data_root)


def test_user_data_inventory_records_reparse_entry_without_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch profile junctions being followed outside the approved cryptographic inventory root."""
    data_root = tmp_path / "profile"
    reparse = data_root / "junction"
    hidden = reparse / "outside.txt"
    reparse.mkdir(parents=True)
    hidden.write_bytes(b"must-not-be-traversed")
    original_lstat = os.lstat
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

    class ReparseMetadata:
        def __init__(self, source: os.stat_result) -> None:
            self.st_mode = source.st_mode
            self.st_size = source.st_size
            self.st_mtime_ns = source.st_mtime_ns
            self.st_file_attributes = getattr(source, "st_file_attributes", 0) | reparse_flag

    def marked_lstat(path: os.PathLike[str] | str, *args: object, **kwargs: object) -> Any:
        metadata = original_lstat(path, *args, **kwargs)
        return ReparseMetadata(metadata) if Path(path) == reparse else metadata

    monkeypatch.setattr(os, "lstat", marked_lstat)

    inventory = {entry[0]: entry[1:] for entry in _user_data_inventory(data_root)}

    assert inventory["junction"][0] == "reparse"
    assert "junction/outside.txt" not in inventory


def test_short_root_is_identity_checked_and_removed(tmp_path: Path) -> None:
    """Catch test output roots that leak artifacts or permit unverified recursive cleanup."""
    with _short_root(tmp_path, "cleanup") as root:
        assert root.is_dir()
        assert (root / ".task10-owner.json").is_file()
        (root / "artifact.bin").write_bytes(b"temporary")

    assert not root.exists()


def test_retail_corpus_manifest_is_exact_before_launch(repository_root: Path) -> None:
    """Catch additions, removals, renames, truncation, or byte changes before engine launch."""
    replay_root = repository_root / "GeneralsReplays" / "GeneralsZH" / "1.04" / "Replays"

    replays = _retail_corpus(replay_root)

    assert len(replays) == 10


def test_isolated_user_data_stages_exact_pinned_custom_maps(
    tmp_path: Path,
    repository_root: Path,
) -> None:
    """Catch corpus setup omitting or silently changing a custom-map member."""
    destination = (tmp_path / "isolated-user-data").resolve()

    staged = _stage_isolated_user_data(repository_root, destination)

    assert staged == destination
    assert _relative_file_manifest(staged / "Maps") == _CUSTOM_MAP_MANIFEST


def test_pinned_gate_binds_an_isolated_user_data_root_to_both_launch_modes() -> None:
    """Catch the high-value 3x3 gate launching through the registry-derived real profile."""
    source = textwrap.dedent(inspect.getsource(test_pinned_replay_three_runs_are_deterministic_and_non_interfering))
    tree = ast.parse(source)
    launch_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"_run_disabled", "_run_enabled"}
    ]

    assert len(launch_calls) == 2
    assert all(any(keyword.arg == "replay_user_data_root" for keyword in call.keywords) for call in launch_calls)


def test_loaded_user_map_identity_excludes_the_selected_absolute_profile_root(repository_root: Path) -> None:
    """Catch textual prefix rewriting accepting traversal/sibling escapes or locale-dependent identities."""
    source = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayTelemetry.cpp"
    ).read_text(encoding="utf-8")
    canonicalizer = source.split("void normalizePathSeparators", maxsplit=1)[1].split(
        "class Sha256", maxsplit=1
    )[0]

    assert "canonicalReplayMapIdentity" in source
    assert '"userdata/maps/"' in source
    assert "TheGlobalData->getPath_UserData()" in source
    assert canonicalizer.count("GetFullPathNameA") == 1
    assert canonicalizer.count("GetFinalPathNameByHandleA") == 1
    assert "CreateFileA(value, FILE_READ_ATTRIBUTES" in canonicalizer
    assert canonicalizer.count("canonicalAbsolutePath(") >= 4
    assert "isPathComponentWithin" in canonicalizer
    assert "isTextualUserMapsCandidate" in canonicalizer
    assert 'path.compare(cursor, 2, ".\\\\")' in canonicalizer
    assert 'path.compare(cursor, separator - cursor, "maps")' in canonicalizer
    assert "textualUserMapsCandidate" in canonicalizer
    assert "candidateEscapesUserMaps" in canonicalizer
    assert "isPathComponentWithin(finalMapPath, finalUserMaps)" in canonicalizer
    assert "std::tolower" not in canonicalizer
    assert "asciiLowerPath" in canonicalizer
    assert "canonicalReplayMapIdentity(TheRecorder->getGameInfo()->getMap(), s_mapIdentity)" in source
    assert 'setWriterError("map_identity_path_escape"' in source


def test_verification_document_hash_tables_are_exact_and_cross_bound(repository_root: Path) -> None:
    """Catch malformed or contradictory authoritative hashes in the Task 10 handoff."""
    document = (repository_root / "docs/replay-analyzer/telemetry-verification.md").read_text(encoding="utf-8")
    expected = _expected_evidence()
    top_table = document.split("| # | Replay |", maxsplit=1)[1].split(
        "All six emitted-file hashes", maxsplit=1
    )[0]
    member_table = document.split("| # | `height.f32.zlib` |", maxsplit=1)[1].split(
        "The isolated map manifest", maxsplit=1
    )[0]

    def rows(table: str) -> dict[int, list[str]]:
        parsed: dict[int, list[str]] = {}
        for line in table.splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if cells and cells[0].isdigit():
                parsed[int(cells[0])] = cells
        return parsed

    top_rows = rows(top_table)
    member_rows = rows(member_table)
    assert set(top_rows) == set(member_rows) == set(range(1, 11))
    for index in range(1, 11):
        expected_run = expected["corpus"][index - 1]
        expected_map = expected_run["enabled"]["map"]
        top_hashes = [value.strip("`") for value in top_rows[index][3:4] + top_rows[index][5:8]]
        member_hashes = [value.strip("`") for value in member_rows[index][1:]]
        assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in (*top_hashes, *member_hashes))
        assert top_rows[index][7] == member_rows[index][2]
        assert top_rows[index][1] == f"`{expected_run['replay']}`"
        assert top_rows[index][5] == f"`{expected_run['enabled']['normalized_trace_sha256']}`"
        assert top_rows[index][6] == f"`{expected_map['content_sha256']}`"
        assert top_rows[index][7] == f"`{expected_map['manifest_sha256']}`"
        expected_terminal = expected_run["terminal"]
        assert top_rows[index][4] == (
            f"`{expected_terminal['exit_code']},"
            f"{str(expected_terminal['playback_started']).lower()},"
            f"{expected_terminal['final_frame']},"
            f"{expected_terminal['command_count']},"
            f"{expected_terminal['terminal_reason']},"
            f"{expected_terminal['crc_mismatch_frame']}`"
        )
        assert member_hashes == [expected_map["file_sha256"][name] for name in (
            "height.f32.zlib",
            "manifest.json",
            "pathing-amphibious.u8.zlib",
            "pathing-ground.u8.zlib",
            "terrain.u8.zlib",
            "zones.i32.zlib",
        )]

    pinned = expected["pinned"]["enabled"]
    assert pinned["normalized_trace_sha256"] in document
    assert pinned["map"]["content_sha256"] in document
    assert pinned["map"]["manifest_sha256"] in document
    for value in expected["provenance"].values():
        assert value in document


def test_pinned_replay_three_runs_are_deterministic_and_non_interfering(
    tmp_path: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Catch telemetry changing replay facts or emitting nondeterministic authoritative evidence."""
    replay_sha256 = _sha256_file(pinned_replay)
    executable_sha256 = _sha256_file(zero_hour_runtime_executable)
    user_data_before = _user_data_inventory()
    disabled: list[RunEvidence] = []
    enabled: list[RunEvidence] = []
    try:
        with _short_root(tmp_path, "pinned") as root:
            for index in range(3):
                disabled_user_data = _stage_pinned_replay_user_data(root / f"ud{index}")
                enabled_user_data = _stage_pinned_replay_user_data(root / f"ue{index}")
                disabled.append(
                    _run_disabled(
                        zero_hour_runtime_executable,
                        pinned_replay,
                        root / f"d{index}",
                        run_id=f"00000000-0000-4000-8000-{index:012d}",
                        replay_user_data_root=disabled_user_data,
                    )
                )
                enabled.append(
                    _run_enabled(
                        zero_hour_runtime_executable,
                        pinned_replay,
                        root / f"e{index}",
                        run_id=f"10000000-0000-4000-8000-{index:012d}",
                        replay_user_data_root=enabled_user_data,
                    )
                )

            assert len({_terminal_facts(run.outcome) for run in disabled}) == 1
            assert len({_terminal_facts(run.outcome) for run in enabled}) == 1
            assert {_terminal_facts(run.outcome) for run in disabled} == {
                _terminal_facts(run.outcome) for run in enabled
            }
            assert len({run.exit_code for run in (*disabled, *enabled)}) == 1
            assert len({run.stdout for run in (*disabled, *enabled)}) == 1
            assert len({run.stderr for run in (*disabled, *enabled)}) == 1
            assert len({run.normalized_trace_sha256 for run in enabled}) == 1
            assert len({run.map_evidence for run in enabled}) == 1
            assert all(run.player_names == ("leex279", "FOX27") for run in enabled)
            assert all(run.match_outcome is not None for run in enabled)
            assert len({json.dumps(run.match_outcome, sort_keys=True) for run in enabled}) == 1
            assert all(
                run.match_outcome is not None
                and run.match_outcome["status"] == "unknown"
                and run.match_outcome["source"] == "unavailable"
                and run.match_outcome["winner_player_indices"] == []
                and run.match_outcome["loser_player_indices"] == []
                for run in enabled
            )
            print(json.dumps([_summary(pinned_replay, "disabled", run) for run in disabled]))
            print(json.dumps([_summary(pinned_replay, "enabled", run) for run in enabled]))
            expected = _expected_evidence()["pinned"]
            assert all(_terminal_evidence(run) == expected["terminal"] for run in (*disabled, *enabled))
            assert all(_enabled_evidence(run) == expected["enabled"] for run in enabled)
    finally:
        assert _sha256_file(pinned_replay) == replay_sha256
        assert _sha256_file(zero_hour_runtime_executable) == executable_sha256
        assert _user_data_inventory() == user_data_before


def test_terrain_road_bridge_has_map_loaded_lifecycle_identity(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
) -> None:
    """Catch a terrain-road bridge whose exported object identity cannot join map and lifecycle evidence."""
    replay = _retail_corpus(
        repository_root / "GeneralsReplays" / "GeneralsZH" / "1.04" / "Replays"
    )[1]
    replay_sha256 = _sha256_file(replay)
    executable_sha256 = _sha256_file(zero_hour_runtime_executable)
    user_data_before = _user_data_inventory()
    try:
        with _short_root(tmp_path, "bridge") as root:
            isolated_user_data = _stage_isolated_user_data(repository_root, root / "user-data")
            evidence = _run_enabled(
                zero_hour_runtime_executable,
                replay,
                root / "enabled",
                run_id="40000000-0000-4000-8000-000000000000",
                replay_user_data_root=isolated_user_data,
            )

            assert evidence.outcome.playback_started is True
            assert evidence.map_evidence is not None
            assert evidence.runtime_map_facts is not None
            assert len(evidence.runtime_map_facts.bridge_bindings) == 2
            assert all(
                binding.template_name == "GenericBridge"
                and binding.initialization_snapshot_status == "present"
                and binding.orientation == pytest.approx(binding.orientation_from_map_endpoints, abs=1e-6)
                for binding in evidence.runtime_map_facts.bridge_bindings
            )
    finally:
        assert _sha256_file(replay) == replay_sha256
        assert _sha256_file(zero_hour_runtime_executable) == executable_sha256
        assert _user_data_inventory() == user_data_before


def test_map_loaded_unclassified_immobile_outside_pathfinder_bounds_is_source_exempt(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
) -> None:
    """Catch legitimate map decoration outside active pathing being rejected or broadly exempted."""
    replay = _retail_corpus(
        repository_root / "GeneralsReplays" / "GeneralsZH" / "1.04" / "Replays"
    )[2]
    replay_sha256 = _sha256_file(replay)
    executable_sha256 = _sha256_file(zero_hour_runtime_executable)
    user_data_before = _user_data_inventory()
    try:
        with _short_root(tmp_path, "shrubbery") as root:
            isolated_user_data = _stage_isolated_user_data(repository_root, root / "user-data")
            evidence = _run_enabled(
                zero_hour_runtime_executable,
                replay,
                root / "enabled",
                run_id="50000000-0000-4000-8000-000000000000",
                replay_user_data_root=isolated_user_data,
            )

            assert evidence.outcome.playback_started is True
            assert evidence.map_evidence is not None
            assert evidence.runtime_map_facts is not None
            assert tuple(
                (sample.object_id, sample.template_name)
                for sample in evidence.runtime_map_facts.oob_exemptions
            ) == (
                (262, "TreePalm1"),
                (347, "TreePalm2"),
                (364, "TreePalm1"),
                (365, "TreePalm1"),
                (366, "TreePalm1"),
                (490, "RocksG14"),
                (492, "RocksG14"),
                (502, "RocksG08"),
                (542, "TreePalm04"),
                (565, "TreePalm1"),
                (569, "TreePalm04"),
                (585, "TreePalm04"),
                (589, "TreePalm04"),
                (592, "TreePalm2short"),
                (593, "TreePalm2short"),
                (708, "TreePalm04"),
            )
            assert evidence.runtime_map_facts.duplicate_label_edges == ()
    finally:
        assert _sha256_file(replay) == replay_sha256
        assert _sha256_file(zero_hour_runtime_executable) == executable_sha256
        assert _user_data_inventory() == user_data_before


def test_all_retail_replays_match_with_telemetry_disabled_and_enabled(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
) -> None:
    """Catch telemetry changing terminal replay facts anywhere in the complete 1.04 retail corpus."""
    replay_root = repository_root / "GeneralsReplays" / "GeneralsZH" / "1.04" / "Replays"
    replays = _retail_corpus(replay_root)
    replay_hashes = {replay: _sha256_file(replay) for replay in replays}
    executable_sha256 = _sha256_file(zero_hour_runtime_executable)
    user_data_before = _user_data_inventory()
    summaries: list[dict[str, object]] = []
    corpus_evidence: list[dict[str, object]] = []
    duplicate_label_edges: dict[str, tuple[WaypointEdgeEvidence, ...]] = {}
    try:
        with _short_root(tmp_path, "corpus") as root:
            for index, replay in enumerate(replays):
                disabled_user_data = _stage_isolated_user_data(repository_root, root / f"ud{index}")
                enabled_user_data = _stage_isolated_user_data(repository_root, root / f"ue{index}")
                disabled = _run_disabled(
                    zero_hour_runtime_executable,
                    replay.resolve(),
                    root / f"d{index}",
                    run_id=f"20000000-0000-4000-8000-{index:012d}",
                    replay_user_data_root=disabled_user_data,
                )
                enabled = _run_enabled(
                    zero_hour_runtime_executable,
                    replay.resolve(),
                    root / f"e{index}",
                    run_id=f"30000000-0000-4000-8000-{index:012d}",
                    replay_user_data_root=enabled_user_data,
                )
                assert _terminal_facts(enabled.outcome) == _terminal_facts(disabled.outcome), replay.name
                assert enabled.exit_code == disabled.exit_code, replay.name
                assert enabled.stdout == disabled.stdout, replay.name
                assert enabled.stderr == disabled.stderr, replay.name
                assert enabled.map_evidence is not None
                assert enabled.runtime_map_facts is not None
                if enabled.runtime_map_facts.duplicate_label_edges:
                    duplicate_label_edges[replay.name] = enabled.runtime_map_facts.duplicate_label_edges
                corpus_evidence.append(
                    {
                        "replay": replay.name,
                        "terminal": _terminal_evidence(disabled),
                        "enabled": _enabled_evidence(enabled),
                    }
                )
                summaries.extend((_summary(replay, "disabled", disabled), _summary(replay, "enabled", enabled)))

            assert {
                replay_name: tuple(
                    (
                        edge.source_waypoint_id,
                        edge.source_name,
                        edge.target_waypoint_id,
                        edge.target_name,
                    )
                    for edge in edges
                )
                for replay_name, edges in duplicate_label_edges.items()
            } == {
                "00-41-30_2v2_Nic_BOMD2MAS_HardAI_HardAI.rep": (
                    (32, "Waypoint 128", 31, "Waypoint 128"),
                ),
                "05-01-50_2v2_amoor123_beshr_HardAI_HardAI.rep": (
                    (32, "Waypoint 128", 31, "Waypoint 128"),
                ),
                "15-07-24_2v2v2_Emkill_haker_HardAI_HardAI_HardAI_HardAI.rep": (
                    (11, "Waypoint 50", 10, "Waypoint 51"),
                    (12, "Waypoint 51", 11, "Waypoint 50"),
                    (14, "Waypoint 47", 13, "Waypoint 48"),
                    (15, "Waypoint 50", 14, "Waypoint 47"),
                    (17, "Waypoint 44", 16, "Waypoint 45"),
                    (18, "Waypoint 49", 17, "Waypoint 44"),
                    (22, "Waypoint 47", 21, "Waypoint 48"),
                    (23, "Waypoint 46", 22, "Waypoint 47"),
                    (25, "Waypoint 44", 24, "Waypoint 45"),
                    (26, "Waypoint 43", 25, "Waypoint 44"),
                ),
            }
            print(json.dumps(summaries))
            assert corpus_evidence == _expected_evidence()["corpus"]
    finally:
        assert {replay: _sha256_file(replay) for replay in replays} == replay_hashes
        assert _relative_file_manifest(repository_root / "GeneralsReplays/GeneralsZH/1.04/Maps") == (
            _CUSTOM_MAP_MANIFEST
        )
        assert _sha256_file(zero_hour_runtime_executable) == executable_sha256
        assert _user_data_inventory() == user_data_before
