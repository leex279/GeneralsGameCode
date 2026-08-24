"""Immutable application settings and product-owned runtime paths."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self

from platformdirs import PlatformDirs
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from generals_replay_analyzer.configuration import ConfigurationStore, EffectiveSettingsSnapshot

_ENV_PREFIX = "GENERALS_REPLAY_ANALYZER_"
_PERSISTED_SAFE_SETTING_KEYS = (
    "import_mode",
    "minimum_longitudinal_sample_size",
    "movement_sample_frames",
    "ollama_model",
    "ollama_url",
)
_ALLOW_REPOSITORY_OUTPUTS_FOR_TESTING: ContextVar[bool] = ContextVar(
    "allow_repository_outputs_for_testing",
    default=False,
)


def _default_data_root() -> Path:
    """Return the platform product directory without creating it."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "GeneralsReplayAnalyzer"
    return Path(PlatformDirs("GeneralsReplayAnalyzer", appauthor=False, roaming=False).user_data_path)


def _absolute_path(value: Path) -> Path:
    """Normalize a configured path without requiring or creating it."""
    return value.expanduser().resolve(strict=False)


def _containing_git_checkout(path: Path) -> Path | None:
    """Find a linked or ordinary Git checkout containing a prospective output path."""
    for candidate in (path, *path.parents):
        marker = candidate / ".git"
        if marker.is_file() or marker.is_dir():
            return candidate
    return None


# TheSuperHackers @feature Leex 21/08/2026 Centralize analyzer paths without creating runtime data during configuration. (#TBD)
class AnalyzerSettings(BaseSettings):
    """Frozen application configuration shared by import, analysis, and presentation stages."""

    model_config = SettingsConfigDict(
        env_prefix=_ENV_PREFIX,
        env_ignore_empty=True,
        extra="forbid",
        frozen=True,
    )

    data_root: Path
    database_path: Path
    managed_replay_directory: Path
    map_asset_directory: Path
    cache_directory: Path
    log_directory: Path
    engine_executable: Path | None = None
    # TheSuperHackers @feature Leex 24/08/2026 Launch development builds beside the installed Zero Hour runtime data. (#TBD)
    engine_runtime_directory: Path | None = None
    # TheSuperHackers @feature Leex 24/08/2026 Bind production replay map lookup to an explicit retail user-data root. (#TBD)
    engine_user_data_directory: Path | None = None
    ollama_url: str = Field(default="http://127.0.0.1:11434", min_length=1)
    ollama_model: str = Field(default="qwen3.6:27b", min_length=1)
    watched_folders: tuple[Path, ...] = ()
    import_mode: Literal["copy", "reference"] = "copy"
    minimum_longitudinal_sample_size: int = Field(default=5, ge=1)
    # TheSuperHackers @feature Leex 23/08/2026 Unify the effective movement sampling interval across analyzer stages. (#TBD)
    movement_sample_frames: int = Field(default=15, ge=1, le=3600)
    # TheSuperHackers @feature Leex 24/08/2026 Keep video executables and closed production settings outside Web request control. (#TBD)
    ffmpeg_executable: Path | None = None
    ffprobe_executable: Path | None = None
    video_voice_provider: Literal["windows_sapi"] = "windows_sapi"
    video_voice_name: str = Field(default="Microsoft Zira Desktop", min_length=1, max_length=160)
    video_width: int = Field(default=1280, ge=640, le=7680)
    video_height: int = Field(default=720, ge=360, le=4320)
    video_fps: Literal[30, 60] = 30
    video_subtitle_mode: Literal["track", "burned"] = "track"

    @classmethod
    def _for_testing_with_repository_outputs(cls, **values: Any) -> Self:
        """Construct settings with checkout outputs enabled only for isolated tests."""
        token = _ALLOW_REPOSITORY_OUTPUTS_FOR_TESTING.set(True)
        try:
            return cls(**values)
        finally:
            _ALLOW_REPOSITORY_OUTPUTS_FOR_TESTING.reset(token)

    @model_validator(mode="before")
    @classmethod
    def _reject_unknown_prefixed_environment(cls, value: Any) -> Any:
        """Fail fast on misspelled product environment settings."""
        known_names = {f"{_ENV_PREFIX}{field_name}".upper() for field_name in cls.model_fields}
        unknown_names = sorted(
            name
            for name in os.environ
            if name.upper().startswith(_ENV_PREFIX) and name.upper() not in known_names
        )
        if unknown_names:
            joined_names = ", ".join(unknown_names)
            raise ValueError(f"Unknown {_ENV_PREFIX} environment setting(s): {joined_names}")
        return value

    @model_validator(mode="before")
    @classmethod
    def _derive_owned_paths(cls, value: Any) -> Any:
        """Fill product path defaults from one root before required fields are validated."""
        if not isinstance(value, Mapping):
            return value
        values = dict(value)
        root_value = values.get("data_root", _default_data_root())
        root = Path(root_value)
        values.setdefault("data_root", root)
        values.setdefault("database_path", root / "replay-analyzer.sqlite3")
        values.setdefault("managed_replay_directory", root / "replays")
        values.setdefault("map_asset_directory", root / "map-assets-v1")
        values.setdefault("cache_directory", root / "cache")
        values.setdefault("log_directory", root / "logs")
        return values

    @model_validator(mode="after")
    def _normalize_and_protect_paths(self) -> AnalyzerSettings:
        """Freeze canonical paths and reject every repository-owned output location."""
        output_fields = (
            "data_root",
            "database_path",
            "managed_replay_directory",
            "map_asset_directory",
            "cache_directory",
            "log_directory",
        )
        for field_name in output_fields:
            normalized = _absolute_path(getattr(self, field_name))
            object.__setattr__(self, field_name, normalized)
            if not _ALLOW_REPOSITORY_OUTPUTS_FOR_TESTING.get():
                checkout = _containing_git_checkout(normalized)
                if checkout is not None:
                    raise ValueError(f"{field_name} points inside a Git checkout: {checkout}")

        if self.engine_executable is not None:
            object.__setattr__(self, "engine_executable", _absolute_path(self.engine_executable))
        if self.engine_runtime_directory is not None:
            object.__setattr__(self, "engine_runtime_directory", _absolute_path(self.engine_runtime_directory))
        if self.engine_user_data_directory is not None:
            object.__setattr__(self, "engine_user_data_directory", _absolute_path(self.engine_user_data_directory))
        for field_name in ("ffmpeg_executable", "ffprobe_executable"):
            executable = getattr(self, field_name)
            if executable is not None:
                object.__setattr__(self, field_name, _absolute_path(executable))
        if self.video_width % 2 or self.video_height % 2:
            raise ValueError("video dimensions must be even for yuv420p output")
        object.__setattr__(self, "watched_folders", tuple(_absolute_path(path) for path in self.watched_folders))
        return self

    @property
    def run_directory(self) -> Path:
        """Return the fixed Task 9 transaction parent below the configured product root."""
        return self.data_root / "runs"

    @property
    def video_run_directory(self) -> Path:
        """Return the fixed product-owned parent for isolated video render transactions."""
        return self.data_root / "video-runs"

    def ensure_directories(self) -> None:
        """Create only analyzer-owned output directories, never caller input locations."""
        directories = (
            self.data_root,
            self.database_path.parent,
            self.managed_replay_directory,
            self.run_directory,
            self.video_run_directory,
            self.map_asset_directory,
            self.cache_directory,
            self.log_directory,
        )
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True, slots=True)
class RuntimeConfiguration:
    """One process-lifetime settings object bound to its exact configuration identity."""

    settings: AnalyzerSettings
    store: ConfigurationStore
    snapshot: EffectiveSettingsSnapshot


# TheSuperHackers @feature Leex 23/08/2026 Activate persisted safe settings through one process-lifetime configuration identity. (#TBD)
def load_runtime_configuration(
    *,
    configuration_root: Path | None = None,
    environment: Mapping[str, str] | None = None,
    values: Mapping[str, object] | None = None,
    version_identities: Sequence[tuple[str, str]] = (),
) -> RuntimeConfiguration:
    """Resolve programmatic, environment, persisted, and default settings exactly once."""

    from generals_replay_analyzer.configuration import ConfigurationStore

    supplied = dict(values or {})
    composition_overrides = {
        key: supplied[key]
        for key in _PERSISTED_SAFE_SETTING_KEYS
        if key in supplied
    }
    store = ConfigurationStore(
        configuration_root=configuration_root,
        environment=environment,
        composition_overrides=composition_overrides,
        version_identities=version_identities,
    )
    snapshot = store.read()
    resolved = dict(supplied)
    for key in _PERSISTED_SAFE_SETTING_KEYS:
        resolved[key] = snapshot.value(key)

    if environment is not None:
        known_environment = {
            f"{_ENV_PREFIX}{field_name}".upper(): field_name
            for field_name in AnalyzerSettings.model_fields
        }
        unknown = tuple(
            name
            for name in environment
            if name.upper().startswith(_ENV_PREFIX) and name.upper() not in known_environment
        )
        if unknown:
            raise ValueError("Unknown analyzer environment setting")
        for environment_name, field_name in known_environment.items():
            raw = environment.get(environment_name)
            if (
                field_name not in _PERSISTED_SAFE_SETTING_KEYS
                and field_name not in supplied
                and raw is not None
                and raw != ""
            ):
                resolved[field_name] = raw

    settings = AnalyzerSettings.model_validate(resolved)
    return RuntimeConfiguration(settings=settings, store=store, snapshot=snapshot)
