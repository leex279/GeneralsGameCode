"""Source contract for the modern Zero Hour replay camera director."""

from pathlib import Path


def _read(repository_root: Path, relative_path: str) -> str:
    path = repository_root / relative_path
    assert path.is_file(), f"missing native camera file: {relative_path}"
    return path.read_text(encoding="utf-8")


def test_autocamera_switch_is_modern_zero_hour_only_and_validated_before_startup(
    repository_root: Path,
) -> None:
    command_line = _read(repository_root, "Core/GameEngine/Source/Common/CommandLine.cpp")
    global_header = _read(repository_root, "GeneralsMD/Code/GameEngine/Include/Common/GlobalData.h")
    base_global_header = _read(repository_root, "Generals/Code/GameEngine/Include/Common/GlobalData.h")
    cmake = _read(repository_root, "CMakeLists.txt")

    assert '{ "-autocamera", parseAutoCamera }' in command_line
    camera_parser = command_line.split("Int parseAutoCamera", maxsplit=1)[1].split(
        "Int ", maxsplit=1
    )[0]
    assert "num <= 1" in camera_parser
    assert "s_autoCameraScript" in camera_parser
    assert "return 2" in camera_parser
    assert "validateAutoCameraOptions();" in command_line
    assert "AutoCameraDirector::validateCameraScript" in command_line
    assert "defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)" in command_line
    assert "m_autoCameraScript" not in global_header
    assert "m_autoCameraScript" not in base_global_header

    zero_hour_block = cmake.split("if(RTS_BUILD_ZEROHOUR)", maxsplit=1)[1].split(
        "if(RTS_BUILD_GENERALS)", maxsplit=1
    )[0]
    assert "if(NOT IS_VS6_BUILD)" in zero_hour_block
    assert "Core/GameEngine/Source/GameClient/AutoCameraDirector.cpp" in zero_hour_block
    assert "target_sources(z_gameengine PRIVATE" in zero_hour_block


def test_autocamera_parser_consumes_every_field_and_rejects_invalid_scripts(
    repository_root: Path,
) -> None:
    header = _read(repository_root, "Core/GameEngine/Include/GameClient/AutoCameraDirector.h")
    source = _read(repository_root, "Core/GameEngine/Source/GameClient/AutoCameraDirector.cpp")

    for field in (
        "m_startFrame",
        "m_endFrame",
        "m_targetPos",
        "m_zoom",
        "m_pitch",
        "m_yaw",
        "m_transition",
        "m_segmentId",
    ):
        assert field in header

    assert "EXPECTED_CAMERA_COLUMNS = 10" in source
    assert "parseFrame" in source
    assert "parseFiniteReal" in source
    assert "std::isfinite" in source
    assert "CAMERA_COORDINATE_LIMIT = 10000000.0f" in source
    assert "MIN_CAMERA_ZOOM = 0.1f" in source
    assert "MAX_CAMERA_ZOOM = 10.0f" in source
    assert "MIN_CAMERA_PITCH = -89.0f" in source
    assert "MAX_CAMERA_PITCH = 0.0f" in source
    assert "MIN_CAMERA_YAW = -360.0f" in source
    assert "MAX_CAMERA_YAW = 360.0f" in source
    assert 'transition == "cut"' in source
    assert 'transition == "ease"' in source
    assert "start frame must be zero" in source
    assert "camera rows must be inclusive and gapless" in source
    assert "camera segment IDs must be unique" in source
    assert "camera row has an invalid field count" in source
    assert "camera script contains no rows" in source
    assert "AutoCameraValidationCache *s_validatedCameraCache = nullptr;" in source
    assert "AsciiString s_validatedCameraScript;" not in source
    assert "std::vector<AutoCameraSegment> s_validatedCameraSegments;" not in source
    assert "std::signbit(parsed)" in source
    assert "Ease rows are baked transition windows" in source
    assert "ease transition cannot be the first camera row" in source
    assert "ease transition must span at least two frames" in source
    for index, field in enumerate(
        (
            "m_startFrame",
            "m_endFrame",
            "m_targetPos.x",
            "m_targetPos.y",
            "m_targetPos.z",
            "m_zoom",
            "m_pitch",
            "m_yaw",
        )
    ):
        assert f"columns[{index}]" in source
        assert f"&segment.{field}" in source
    assert "columns[8]" in source and "columns[9]" in source

    init_body = source.split("void AutoCameraDirector::init()", maxsplit=1)[1].split(
        "void AutoCameraDirector::reset()", maxsplit=1
    )[0]
    assert init_body.index("loadCameraScript") < init_body.index("m_enabled = TRUE")

    validate_body = source.split(
        "Bool AutoCameraDirector::validateCameraScript", maxsplit=1
    )[1].split("Bool AutoCameraDirector::loadCameraScript", maxsplit=1)[0]
    load_body = source.split("Bool AutoCameraDirector::loadCameraScript", maxsplit=1)[1].split(
        "Bool AutoCameraDirector::evaluateCameraAtFrame", maxsplit=1
    )[0]
    assert "parseCameraScript" in validate_body
    assert "new AutoCameraValidationCache" in validate_body
    assert "cache->m_segments.swap(parsedSegments)" in validate_body
    assert "parseCameraScript" not in load_body
    assert "m_segments = s_validatedCameraCache->m_segments" in load_body
    assert "delete s_validatedCameraCache;" in load_body
    assert "s_validatedCameraCache = nullptr;" in load_body

    forbidden_parsers = (
        "sscanf(",
        "strtok(",
        "atof(",
        "atoi(",
        "std::ifstream",
        "std::istringstream",
    )
    assert not [token for token in forbidden_parsers if token in source]
    assert "clamp(" not in source


def test_autocamera_interpolation_is_frame_based_seek_safe_and_view_only(
    repository_root: Path,
) -> None:
    header = _read(repository_root, "Core/GameEngine/Include/GameClient/AutoCameraDirector.h")
    source = _read(repository_root, "Core/GameEngine/Source/GameClient/AutoCameraDirector.cpp")

    assert "evaluateCameraAtFrame" in header
    assert "TheGameLogic->getFrame()" in source
    assert "LOGICFRAMES_PER_SECOND" not in source
    assert "m_current" not in header
    assert "timeSec" not in source
    assert "smoothStep" in source
    assert "frame - current.m_startFrame" in source
    assert "current.m_endFrame - current.m_startFrame" in source

    update_body = source.split("void AutoCameraDirector::update()", maxsplit=1)[1]
    assert "TheTacticalView->lookAt(&camera.m_targetPos);" in update_body
    assert "TheTacticalView->setZoom(camera.m_zoom);" in update_body
    assert "TheTacticalView->setPitch(DEG_TO_RADF(-camera.m_pitch));" in update_body
    assert "TheTacticalView->setAngle(DEG_TO_RADF(camera.m_yaw));" in update_body
    assert "setQuitting" not in source
    forbidden_logic_mutations = (
        "TheGameLogic->set",
        "TheGameLogic->destroy",
        "TheGameLogic->reset",
        "TheGameLogic->update",
    )
    assert not [token for token in forbidden_logic_mutations if token in source]


def test_autocamera_uses_gameclient_lifecycle_without_base_generals_wiring(
    repository_root: Path,
) -> None:
    game_client = _read(
        repository_root, "GeneralsMD/Code/GameEngine/Source/GameClient/GameClient.cpp"
    )
    base_game_client = _read(
        repository_root, "Generals/Code/GameEngine/Source/GameClient/GameClient.cpp"
    )

    assert '#include "GameClient/AutoCameraDirector.h"' in game_client
    assert "TheAutoCameraDirector = new AutoCameraDirector;" in game_client
    assert "TheAutoCameraDirector->init();" in game_client
    assert "TheAutoCameraDirector->reset();" in game_client
    assert "TheAutoCameraDirector->UPDATE();" in game_client
    assert "delete TheAutoCameraDirector;" in game_client
    assert "AutoCameraDirector" not in base_game_client

    camera_update = game_client.index("TheAutoCameraDirector->UPDATE();")
    display_update = game_client.index("TheDisplay->UPDATE();", camera_update)
    assert camera_update < display_update
