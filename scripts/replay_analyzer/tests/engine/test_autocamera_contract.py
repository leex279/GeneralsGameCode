"""Source contracts for the modern Zero Hour replay camera director."""

from pathlib import Path


def _read(repository_root: Path, relative_path: str) -> str:
    path = repository_root / relative_path
    assert path.is_file(), f"missing native camera file: {relative_path}"
    return path.read_text(encoding="utf-8")


def _method_body(source: str, signature: str) -> str:
    """Return one C++ method body while respecting nested scopes."""
    start = source.index(signature)
    opening_brace = source.index("{", start)
    depth = 0
    for index in range(opening_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening_brace + 1 : index]
    raise AssertionError(f"unterminated C++ method: {signature}")


def test_autocamera_applies_target_and_orientation_as_one_seek_safe_view_operation(
    repository_root: Path,
) -> None:
    """Reject elevated-target projection through the preceding frame's camera transform."""
    view_header = _read(repository_root, "Core/GameEngine/Include/GameClient/View.h")
    w3d_header = _read(
        repository_root,
        "Core/GameEngineDevice/Include/W3DDevice/GameClient/W3DView.h",
    )
    w3d_source = _read(
        repository_root,
        "Core/GameEngineDevice/Source/W3DDevice/GameClient/W3DView.cpp",
    )
    director_source = _read(
        repository_root,
        "GeneralsMD/Code/GameEngine/Source/GameClient/AutoCameraDirector.cpp",
    )

    assert "virtual void setCameraState(" in view_header
    assert "virtual void setCameraState(" in w3d_header

    update_body = _method_body(director_source, "void AutoCameraDirector::update()")
    assert "TheTacticalView->setCameraState(" in update_body
    assert "TheTacticalView->lookAt(" not in update_body
    assert "TheTacticalView->setZoom(" not in update_body
    assert "TheTacticalView->setPitch(" not in update_body
    assert "TheTacticalView->setAngle(" not in update_body

    state_body = _method_body(w3d_source, "void W3DView::setCameraState(")
    state_setters = (
        state_body.index("setZoom("),
        state_body.index("setPitch("),
        state_body.index("setAngle("),
    )
    intended_ray = state_body.index("buildCameraPosition(")
    projected_target = state_body.index("lookAtUsingDirection(")
    assert max(state_setters) < intended_ray < projected_target
    assert "updateCameraTransform(" not in state_body

    look_at_body = _method_body(w3d_source, "void W3DView::lookAt( const Coord3D *o )")
    assert "m_3DCamera->Un_Project" in look_at_body
    assert "lookAtUsingDirection(" in look_at_body
