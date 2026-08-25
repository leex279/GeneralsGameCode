from __future__ import annotations

from pathlib import Path


def test_visual_map_applies_lighting_before_building_terrain_vertices() -> None:
    repository_root = Path(__file__).resolve().parents[4]
    source = (
        repository_root
        / "Core/GameEngineDevice/Source/W3DDevice/GameClient/W3DTerrainVisual.cpp"
    ).read_text(encoding="utf-8")

    parsed_map = source.index("m_logicHeightMap = NEW WorldHeightMap(pStrm);")
    refresh = source.index("TheWritableGlobalData->setTimeOfDay", parsed_map)
    display_refresh = source.index("TheDisplay->setTimeOfDay", refresh)
    vertex_build = source.index("m_terrainRenderObject->initHeightData", display_refresh)

    assert parsed_map < refresh < display_refresh < vertex_build
