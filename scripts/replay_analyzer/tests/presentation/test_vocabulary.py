"""Tests for honest player-facing names and time formatting."""

from generals_replay_analyzer.presentation.vocabulary import (
    faction_label,
    feature_label,
    format_frame,
    game_label,
    map_label,
    reason_label,
)


def test_known_game_identities_use_zero_hour_player_vocabulary() -> None:
    assert game_label("FactionAmericaAirForceGeneral") == "USA Air Force General"
    assert game_label("AmericaVehicleHumvee") == "Humvee"
    assert game_label("GLAArmsDealer") == "Arms Dealer"


def test_feature_and_reason_codes_are_translated_for_players() -> None:
    assert feature_label("economy.supply_collection_rate") == "Supply income"
    assert reason_label("minimum_sample_not_met") == "More analyzed matches are needed"


def test_unknown_identity_is_readable_but_explicitly_unrecognized() -> None:
    assert game_label("Modded_SuperUnitX") == "Modded Super Unit X (unrecognized)"


def test_faction_label_hides_unresolved_slot_codes_and_names_verified_factions() -> None:
    assert faction_label(None) is None
    for unresolved in (" 7 ", "-1", "+7", "0x7", "7.0"):
        assert faction_label(unresolved) is None
    assert faction_label("FactionAmericaAirForceGeneral") == "USA Air Force General"
    assert faction_label("FactionChinaTankGeneral") == "China Tank General"
    assert faction_label("FactionGLAToxinGeneral") == "GLA Toxin General"
    assert faction_label("GLA") == "GLA"


def test_map_label_removes_storage_paths_and_rank_tags_without_changing_named_maps() -> None:
    assert map_label("userdata/maps/[rank] sand scorpion") == "Sand Scorpion"
    assert map_label(r"UserData\Maps\Custom_Map.map") == "Custom Map"
    assert map_label("Tournament Desert") == "Tournament Desert"


def test_frame_format_preserves_exact_evidence_location() -> None:
    assert format_frame(105) == "0:03.5 (frame 105)"
