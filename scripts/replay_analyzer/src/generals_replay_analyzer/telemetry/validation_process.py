"""Private process entry point for memory-isolated telemetry bundle validation."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

from .compatibility import bridge_v2_damage_victim_template_name
from .reader import load_validated_telemetry_bundle

_SIDECAR_DIRECTORY = ".validated-telemetry"
_BRIDGE_FILENAME = "victim-templates.json"
_MAP_ASSET_FILENAME = "map-asset.json"


def _relative(root: Path, path: Path | None) -> str | None:
    return None if path is None else path.relative_to(root).as_posix()


def _validated_trace(root_argument: str, trace_argument: str) -> tuple[Path, Path]:
    root = Path(root_argument).resolve(strict=True)
    trace = root / Path(*trace_argument.split("/"))
    info = trace.lstat()
    resolved = trace.resolve(strict=True)
    if (
        not trace_argument
        or trace_argument.startswith("/")
        or "\\" in trace_argument
        or ":" in trace_argument
        or any(part in {"", ".", ".."} for part in trace_argument.split("/"))
        or not stat.S_ISREG(info.st_mode)
        or trace.is_symlink()
        or resolved != trace
        or root not in resolved.parents
    ):
        raise ValueError("telemetry validation trace path is unsafe")
    return root, trace


def _execute(root_argument: str, trace_argument: str) -> dict[str, object]:
    root, trace = _validated_trace(root_argument, trace_argument)
    source_trace_sha256, victim_templates = bridge_v2_damage_victim_template_name(trace)
    bundle = load_validated_telemetry_bundle(trace, retain_records=False)
    sidecar_root = root / _SIDECAR_DIRECTORY
    sidecar_root.mkdir(exist_ok=False)
    bridge_relative_path: str | None = None
    if victim_templates:
        bridge_path = sidecar_root / _BRIDGE_FILENAME
        bridge_path.write_text(
            json.dumps(victim_templates, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        bridge_relative_path = bridge_path.relative_to(root).as_posix()
    map_asset_relative_path: str | None = None
    if bundle.map_asset is not None:
        map_asset_path = sidecar_root / _MAP_ASSET_FILENAME
        map_asset_path.write_text(
            json.dumps(bundle.map_asset.model_dump(mode="json"), separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        map_asset_relative_path = map_asset_path.relative_to(root).as_posix()
    # TheSuperHackers @performance Leex 26/08/2026 Return only bounded metadata before releasing the validator heap. (#TBD)
    return {
        "manifest": bundle.manifest.model_dump(mode="json"),
        "complete": bundle.complete.model_dump(mode="json"),
        "logic_frames_per_second": bundle.logic_frames_per_second,
        "catalog_relative_path": _relative(root, bundle.catalog_path),
        "map_manifest_relative_path": _relative(root, bundle.map_manifest_path),
        "map_member_relative_paths": [_relative(root, path) for path in bundle.map_member_paths],
        "map_asset_relative_path": map_asset_relative_path,
        "bridge_relative_path": bridge_relative_path,
        "source_trace_sha256": source_trace_sha256,
    }


def main(arguments: list[str]) -> int:
    if len(arguments) != 2:
        return 2
    try:
        document = _execute(arguments[0], arguments[1])
    except Exception as error:  # noqa: BLE001 - the process boundary must normalize every validation failure.
        print(
            json.dumps(
                {"error_message": str(error), "error_type": type(error).__name__},
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(document, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
