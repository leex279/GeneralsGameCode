from __future__ import annotations

from pathlib import Path


def _read(repository_root: Path) -> str:
    return (repository_root / "Core/GameEngine/Source/GameClient/RadiusDecal.cpp").read_text(
        encoding="utf-8"
    )


def test_radius_decal_preserves_logic_state_without_render_shadow_manager(
    repository_root: Path,
) -> None:
    source = _read(repository_root)
    create = source.split("void RadiusDecalTemplate::createRadiusDecal", 1)[1].split(
        "void RadiusDecalTemplate::xferRadiusDecalTemplate", 1
    )[0]

    nonempty = create.index("result.m_empty = false;")
    guard = create.index("if (TheProjectedShadowManager == nullptr)")
    allocation = create.index("TheProjectedShadowManager->addDecal")

    assert nonempty < guard < allocation
    assert "return;" in create[guard:allocation]


def test_radius_decal_runtime_methods_remain_null_safe_without_visual_decal(
    repository_root: Path,
) -> None:
    source = _read(repository_root)

    clear = source.split("void RadiusDecal::clear()", 1)[1].split(
        "RadiusDecal::~RadiusDecal", 1
    )[0]
    update = source.split("void RadiusDecal::update()", 1)[1].split(
        "void RadiusDecal::setOpacity", 1
    )[0]
    set_opacity = source.split("void RadiusDecal::setOpacity", 1)[1].split(
        "void RadiusDecal::setPosition", 1
    )[0]
    set_position = source.split("void RadiusDecal::setPosition", 1)[1]

    assert "if (m_decal)" in clear
    assert "if (m_decal && m_template)" in update
    assert "if (m_decal)" in set_opacity
    assert "if (m_decal)" in set_position
