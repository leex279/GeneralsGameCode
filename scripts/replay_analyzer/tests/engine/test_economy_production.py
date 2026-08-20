"""Real-engine contracts for passive economy and production observations."""

import os
import subprocess
from collections import Counter
from itertools import pairwise
from pathlib import Path

import pytest

from generals_replay_analyzer.parser import parse_replay
from generals_replay_analyzer.telemetry.model import (
    CashChangedPayload,
    CashChangedRecord,
    CompleteRecord,
    PlayersInitializedRecord,
    SupplyCollectedRecord,
    TelemetryRecord,
)
from generals_replay_analyzer.telemetry.reader import iter_validated_trace

RUN_ID = "923e4567-e89b-12d3-a456-426614174000"
TASK5_EVENT_TYPES = {
    "production_queued",
    "production_cancelled",
    "production_completed",
    "upgrade_queued",
    "upgrade_cancelled",
    "upgrade_completed",
    "science_purchased",
    "special_power_used",
    "cash_changed",
    "supply_collected",
}


def _environment(repository_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    dependencies = (
        repository_root / "build" / "win32" / "_deps" / "bink-build" / "Release",
        repository_root / "build" / "win32" / "_deps" / "miles-build" / "Release",
    )
    environment["PATH"] = os.pathsep.join([*(str(path.resolve()) for path in dependencies), environment["PATH"]])
    return environment


def _run(executable: Path, replay: Path, trace: Path, repository_root: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [
                str(executable),
                "-headless",
                "-noaudio",
                "-replay",
                str(replay),
                "-telemetry",
                str(trace),
                "-telemetry-run-id",
                RUN_ID,
            ],
            cwd=executable.parent,
            env=_environment(repository_root),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"modern Zero Hour timed out during economy integration: {error}")


def _write_crc_stripped_derivative(source: Path, destination: Path) -> None:
    """Create mechanics-only reachability input without changing the authoritative fixture."""
    parsed = parse_replay(source)
    source_bytes = source.read_bytes()
    pieces = [source_bytes[: parsed.command_stream_offset]]
    pieces.extend(
        source_bytes[command.start_offset : command.end_offset]
        for command in parsed.commands
        if command.message_name != "MSG_LOGIC_CRC"
    )
    destination.write_bytes(b"".join(pieces))


def _assert_cash_fold(records: tuple[TelemetryRecord, ...]) -> None:
    chains: dict[int, list[CashChangedPayload]] = {}
    for record in records:
        if isinstance(record, CashChangedRecord):
            chains.setdefault(record.payload.player_index, []).append(record.payload)
    complete = records[-1]
    assert isinstance(complete, CompleteRecord)
    assert complete.payload.final_cash_balances is not None
    balances = {entry.player_index: entry for entry in complete.payload.final_cash_balances}
    assert balances
    for player_index, chain in chains.items():
        for prior, current in pairwise(chain):
            assert prior.after == current.before
        assert chain[-1].after == balances[player_index].balance


def test_natural_crc_stopping_replay_exposes_engine_cash_chain_and_final_balances(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """Use the unmodified replay only for observations reached before its frame-108 CRC stop."""
    trace = (tmp_path / "natural-economy.ndjson").resolve()
    completed = _run(zero_hour_runtime_executable, pinned_replay, trace, repository_root)

    assert trace.is_file(), completed.stdout[-2000:] + completed.stderr[-2000:]
    records = tuple(iter_validated_trace(trace))
    players = next(record for record in records if isinstance(record, PlayersInitializedRecord))
    complete = records[-1]
    assert isinstance(complete, CompleteRecord)
    assert players.payload.engine_player_indices is not None
    assert complete.payload.final_cash_balances is not None
    final_domain = [entry.player_index for entry in complete.payload.final_cash_balances]
    assert final_domain == players.payload.engine_player_indices
    resolved_slots = {
        slot.player_index
        for slot in players.payload.slots or []
        if slot.player_index is not None
    }
    assert resolved_slots < set(players.payload.engine_player_indices)
    cash = [record for record in records if record.event_type == "cash_changed"]
    counts = Counter(record.event_type for record in records)
    expected = {"production_queued": 1, "cash_changed": 6}
    assert {event_type: counts[event_type] for event_type in TASK5_EVENT_TYPES} == {
        event_type: expected.get(event_type, 0) for event_type in TASK5_EVENT_TYPES
    }
    assert Counter(record.payload.reason for record in cash) == {
        "starting_cash": 4,
        "unit_cost": 1,
        "construction_cost": 1,
    }
    assert all(record.payload.before + record.payload.delta == record.payload.after for record in cash)
    _assert_cash_fold(records)


def test_crc_stripped_derivative_reaches_economy_and_queue_mechanics_without_strategy_claims(
    tmp_path: Path,
    repository_root: Path,
    zero_hour_runtime_executable: Path,
    pinned_replay: Path,
) -> None:
    """The disposable derivative proves mechanics reachability, never natural match history."""
    derivative = tmp_path / "mechanics-only-crc-stripped.rep"
    original = pinned_replay.read_bytes()
    _write_crc_stripped_derivative(pinned_replay, derivative)
    trace = (tmp_path / "mechanics-only.ndjson").resolve()

    completed = _run(zero_hour_runtime_executable, derivative, trace, repository_root)

    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-2000:]
    assert pinned_replay.read_bytes() == original
    records = tuple(iter_validated_trace(trace))
    counts = Counter(record.event_type for record in records)
    expected = {
        "production_queued": 23,
        "production_completed": 23,
        "science_purchased": 2,
        "special_power_used": 1,
        "cash_changed": 864,
        "supply_collected": 794,
    }
    assert {event_type: counts[event_type] for event_type in TASK5_EVENT_TYPES} == {
        event_type: expected.get(event_type, 0) for event_type in TASK5_EVENT_TYPES
    }
    cash_reasons = Counter(
        record.payload.reason for record in records if isinstance(record, CashChangedRecord)
    )
    assert cash_reasons == {
        "starting_cash": 4,
        "unit_cost": 23,
        "construction_cost": 18,
        "supply_income": 794,
        "sell_refund": 1,
        "unknown": 24,
    }
    assert Counter(
        record.payload.science_name for record in records if record.event_type == "science_purchased"
    ) == {"SCIENCE_SpyDrone": 1, "SCIENCE_ScudLauncher": 1}
    assert Counter(
        record.payload.special_power_name for record in records if record.event_type == "special_power_used"
    ) == {"SpecialPowerSpyDrone": 1}
    assert Counter(
        record.payload.template_name for record in records if record.event_type == "production_queued"
    ) == {
        "AirF_AmericaVehicleDozer": 1,
        "AFG_AmericaVehicleChinook": 2,
        "GLAInfantryWorker": 20,
    }
    queued = {
        record.payload.production_id
        for record in records
        if record.event_type == "production_queued"
    }
    terminal = {
        record.payload.production_id
        for record in records
        if record.event_type in {"production_cancelled", "production_completed"}
    }
    assert terminal <= queued
    for index, record in enumerate(records):
        if not isinstance(record, SupplyCollectedRecord):
            continue
        assert record.payload.source_status == "resolved"
        cash = records[index + 1]
        assert isinstance(cash, CashChangedRecord)
        assert cash.frame == record.frame
        assert cash.payload.player_index == record.payload.player_index
        assert cash.payload.delta == record.payload.amount
        assert cash.payload.reason == "supply_income"
    _assert_cash_fold(records)


def test_authoritative_supply_source_is_captured_at_pickup_and_consumed_at_dropoff(repository_root: Path) -> None:
    warehouse = (
        repository_root
        / "GeneralsMD/Code/GameEngine/Source/GameLogic/Object/Update/DockUpdate/SupplyWarehouseDockUpdate.cpp"
    ).read_text(encoding="utf-8")
    center = (
        repository_root
        / "GeneralsMD/Code/GameEngine/Source/GameLogic/Object/Update/DockUpdate/SupplyCenterDockUpdate.cpp"
    ).read_text(encoding="utf-8")

    success = warehouse.split("if( ai && ai->gainOneBox( m_boxesStored ) )", maxsplit=1)[1].split(
        "return TRUE", maxsplit=1
    )[0]
    assert "ReplayEconomy::observeSupplyPickup(docker, getObject())" in success
    assert "ReplayEconomy::observeSupplyCollected" in center
    assert center.index("ReplayEconomy::observeSupplyCollected") < center.index("ownerPlayerMoney->deposit(value)")
    assert "dropoff" in center[center.index("ReplayEconomy::observeSupplyCollected") : center.index("ownerPlayerMoney->deposit(value)")]


def test_special_power_use_is_observed_only_at_the_canonical_committed_trigger(repository_root: Path) -> None:
    source = (
        repository_root
        / "GeneralsMD/Code/GameEngine/Source/GameLogic/Object/SpecialPower/SpecialPowerModule.cpp"
    ).read_text(encoding="utf-8")
    trigger = source.split("void SpecialPowerModule::triggerSpecialPower", maxsplit=1)[1].split(
        "void SpecialPowerModule::createViewObject", maxsplit=1
    )[0]
    intent = source.split("Bool SpecialPowerModule::initiateIntentToDoSpecialPower", maxsplit=1)[1].split(
        "void SpecialPowerModule::triggerSpecialPower", maxsplit=1
    )[0]
    dispatch = source.split("void SpecialPowerModule::doSpecialPower(", maxsplit=1)[1].split(
        "void SpecialPowerModule::pauseCountdown", maxsplit=1
    )[0]

    assert trigger.count("ReplayEconomy::observeSpecialPowerUsed") == 1
    assert "ReplayEconomy::observeSpecialPowerUsed" not in intent
    assert "ReplayEconomy::observeSpecialPowerUsed" not in dispatch
    assert trigger.index("ReplayEconomy::observeSpecialPowerUsed") < trigger.index("startPowerRecharge()")
    assert "triggerSpecialPower( obj->getPosition(), obj )" in dispatch


def test_script_purchase_science_suppresses_only_its_committed_purchase_call(repository_root: Path) -> None:
    script_actions = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/GameLogic/ScriptEngine/ScriptActions.cpp"
    ).read_text(encoding="utf-8")
    purchase_action = script_actions.split("void ScriptActions::doPlayerPurchaseScience", maxsplit=1)[1].split(
        "void ScriptActions::doPlayerSetScienceAvailability", maxsplit=1
    )[0]
    economy = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayEconomy.cpp"
    ).read_text(encoding="utf-8")
    science_observer = economy.split("void ReplayEconomy::observeSciencePurchased", maxsplit=1)[1].split(
        "void ReplayEconomy::observeSpecialPowerUsed", maxsplit=1
    )[0]

    suppression = "ReplaySciencePurchaseSuppressionScope replayScienceSuppression"
    committed_purchase = "pPlayer->attemptToPurchaseScience(science)"
    assert purchase_action.count(suppression) == 1
    assert purchase_action.count(committed_purchase) == 1
    assert purchase_action.index(suppression) < purchase_action.index(committed_purchase)
    assert "sciencePurchaseSuppressionDepth > 0" in science_observer
    assert science_observer.index("sciencePurchaseSuppressionDepth > 0") < science_observer.index(
        "ReplayTelemetry::emit"
    )


def test_players_initialized_freezes_the_full_engine_player_domain(repository_root: Path) -> None:
    source = (
        repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayGameDataExport.cpp"
    ).read_text(encoding="utf-8")
    prepare = source.split("Bool ReplayGameDataExport::prepareCatalog", maxsplit=1)[1].split(
        "void ReplayGameDataExport::emitPlayersInitialized", maxsplit=1
    )[0]

    assert "buildEnginePlayerIndices" in prepare
    assert prepare.index("buildEnginePlayerIndices") < prepare.index("s_playersPayload =")
    assert '\\"engine_player_indices\\"' in prepare


def test_replay_economy_state_is_modern_only_and_retains_no_engine_pointers(repository_root: Path) -> None:
    header = (repository_root / "GeneralsMD/Code/GameEngine/Include/Common/ReplayEconomy.h").read_text(encoding="utf-8")
    source = (repository_root / "GeneralsMD/Code/GameEngine/Source/Common/ReplayEconomy.cpp").read_text(encoding="utf-8")
    guard = "#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)"

    assert guard in header
    assert guard in source
    state = source.split("struct ReplayEconomyState", maxsplit=1)[1].split("};", maxsplit=1)[0]
    assert "Object *" not in state
    assert "Object*" not in state
