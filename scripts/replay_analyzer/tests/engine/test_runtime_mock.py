"""Mocked cleanup contracts for the Windows runtime binding state machine."""

from __future__ import annotations

from pathlib import Path

import pytest

from generals_replay_analyzer.engine import runtime as runtime_module


def _install_binding_mocks(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    source_identity = runtime_module._WindowsFileIdentity(1, 2, 0, 10)
    runtime_identity = runtime_module._WindowsFileIdentity(1, 3, 0x10, 0)
    calls: dict[str, object] = {"staged_opens": 0, "deleted": [], "closed": []}

    monkeypatch.setattr(runtime_module, "_win_open_verified_runtime", lambda _path: (10, runtime_identity))
    monkeypatch.setattr(runtime_module, "_win_open_verified_source", lambda _path, staged: (11, source_identity))
    monkeypatch.setattr(runtime_module, "_win_require_same_volume", lambda _source, _runtime: None)
    monkeypatch.setattr(runtime_module, "_win_create_hardlink", lambda _source, _destination: None)
    monkeypatch.setattr(runtime_module, "_win_revalidate_source", lambda _path, _expected: None)
    monkeypatch.setattr(runtime_module, "_win_close", lambda handle: calls["closed"].append(handle))  # type: ignore[union-attr]
    monkeypatch.setattr(
        runtime_module,
        "_win_mark_delete",
        lambda handle: calls["deleted"].append(handle),  # type: ignore[union-attr]
    )
    return calls


def test_created_link_is_reopened_and_deleted_when_initial_lock_open_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_binding_mocks(monkeypatch)
    first_open = True

    def open_staged(_path: Path, _expected: object) -> int:
        nonlocal first_open
        if first_open:
            first_open = False
            raise OSError("injected lock open failure")
        calls["staged_opens"] = int(calls["staged_opens"]) + 1
        return 12

    monkeypatch.setattr(runtime_module, "_win_open_staged_lock", open_staged)
    source = tmp_path / "build" / "generalszh.exe"
    runtime = tmp_path / "runtime"
    source.parent.mkdir()
    runtime.mkdir()

    with pytest.raises(OSError, match="injected lock open failure"), runtime_module._bind_windows(source, runtime):
        pytest.fail("binding unexpectedly yielded")

    assert calls["staged_opens"] == 1
    assert calls["deleted"] == [12]


def test_primary_body_error_survives_staged_cleanup_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_binding_mocks(monkeypatch)
    monkeypatch.setattr(runtime_module, "_win_open_staged_lock", lambda _path, _expected: 12)
    monkeypatch.setattr(runtime_module, "_win_mark_delete", lambda _handle: (_ for _ in ()).throw(OSError("cleanup")))
    monkeypatch.setattr(
        runtime_module,
        "_win_open_verified_source",
        lambda _path, staged: (11 if staged else 13, runtime_module._WindowsFileIdentity(1, 2, 0, 10)),
    )
    source = tmp_path / "build" / "generalszh.exe"
    runtime = tmp_path / "runtime"
    source.parent.mkdir()
    runtime.mkdir()

    with pytest.raises(RuntimeError, match="launch") as caught, runtime_module._bind_windows(source, runtime):
        raise RuntimeError("launch")

    assert "runtime binding cleanup failed" in "\n".join(caught.value.__notes__ or [])


def test_cleanup_error_surfaces_without_primary_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_binding_mocks(monkeypatch)
    monkeypatch.setattr(runtime_module, "_win_open_staged_lock", lambda _path, _expected: 12)
    monkeypatch.setattr(runtime_module, "_win_mark_delete", lambda _handle: (_ for _ in ()).throw(OSError("cleanup")))
    monkeypatch.setattr(
        runtime_module,
        "_win_open_verified_source",
        lambda _path, staged: (11 if staged else 13, runtime_module._WindowsFileIdentity(1, 2, 0, 10)),
    )
    source = tmp_path / "build" / "generalszh.exe"
    runtime = tmp_path / "runtime"
    source.parent.mkdir()
    runtime.mkdir()

    with pytest.raises(OSError, match="cleanup"), runtime_module._bind_windows(source, runtime):
        pass


def test_source_lock_closes_before_owned_link_is_marked_for_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The remaining staged handle keeps identity authority while the source share lock is released."""
    _install_binding_mocks(monkeypatch)
    events: list[str] = []
    monkeypatch.setattr(runtime_module, "_win_open_staged_lock", lambda _path, _expected: 12)
    monkeypatch.setattr(runtime_module, "_win_mark_delete", lambda handle: events.append(f"delete:{handle}"))
    monkeypatch.setattr(runtime_module, "_win_close", lambda handle: events.append(f"close:{handle}"))
    monkeypatch.setattr(
        runtime_module,
        "_win_open_verified_source",
        lambda _path, staged: (11 if staged else 13, runtime_module._WindowsFileIdentity(1, 2, 0, 10)),
    )
    source = tmp_path / "build" / "generalszh.exe"
    runtime = tmp_path / "runtime"
    source.parent.mkdir()
    runtime.mkdir()

    with runtime_module._bind_windows(source, runtime):
        pass

    assert events.index("close:13") < events.index("delete:12")


def test_staged_cleanup_retries_transient_image_mapping_access_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0
    delays: list[float] = []
    closed: list[int] = []

    def mark_delete(_handle: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError(5, "image mapping is still settling")

    monkeypatch.setattr(runtime_module, "_win_mark_delete", mark_delete)
    monkeypatch.setattr(runtime_module, "_win_close", closed.append)
    monkeypatch.setattr(runtime_module.time, "sleep", delays.append)

    runtime_module._win_cleanup_staged_link(
        Path("staged.exe"),
        runtime_module._WindowsFileIdentity(1, 2, 0, 10),
        12,
    )

    assert attempts == 3
    assert delays == [0.1, 0.1]
    assert closed == [12]
