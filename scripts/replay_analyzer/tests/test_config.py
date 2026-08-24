"""Application configuration and owned runtime-path contracts."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from generals_replay_analyzer.config import AnalyzerSettings, load_runtime_configuration
from generals_replay_analyzer.configuration import ConfigurationStore, SettingChange

PROJECT_ROOT = Path(__file__).parents[3]


def _settings(tmp_path: Path, **changes: object) -> AnalyzerSettings:
    values: dict[str, object] = {"data_root": tmp_path / "product-data"}
    values.update(changes)
    return AnalyzerSettings(_env_file=None, **values)  # type: ignore[arg-type]


def test_default_settings_use_platform_product_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the Windows product default aligned with the accepted engine-runner root."""
    local_app_data = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv("GENERALS_REPLAY_ANALYZER_DATA_ROOT", raising=False)

    settings = AnalyzerSettings(_env_file=None)

    assert settings.data_root == (local_app_data / "GeneralsReplayAnalyzer").resolve()


def test_constructor_overrides_all_configurable_values(tmp_path: Path) -> None:
    """Expose one immutable settings object rather than scattered path and provider knobs."""
    data_root = tmp_path / "data"
    engine = tmp_path / "runtime" / "generalszh.exe"
    engine_runtime = tmp_path / "installed-game"
    watch = tmp_path / "incoming"
    settings = AnalyzerSettings(
        _env_file=None,
        data_root=data_root,
        database_path=tmp_path / "database" / "library.sqlite3",
        managed_replay_directory=tmp_path / "managed-replays",
        map_asset_directory=tmp_path / "managed-maps",
        cache_directory=tmp_path / "model-cache",
        log_directory=tmp_path / "application-logs",
        engine_executable=engine,
        engine_runtime_directory=engine_runtime,
        ollama_url="http://127.0.0.1:22434",
        ollama_model="test-model:latest",
        watched_folders=(watch,),
        import_mode="reference",
        minimum_longitudinal_sample_size=9,
        movement_sample_frames=30,
        ffmpeg_executable=tmp_path / "tools" / "ffmpeg.exe",
        ffprobe_executable=tmp_path / "tools" / "ffprobe.exe",
        video_voice_provider="windows_sapi",
        video_voice_name="Microsoft Zira Desktop",
        video_width=1920,
        video_height=1080,
        video_fps=60,
        video_subtitle_mode="burned",
    )

    assert settings.data_root == data_root.resolve()
    assert settings.database_path == (tmp_path / "database" / "library.sqlite3").resolve()
    assert settings.managed_replay_directory == (tmp_path / "managed-replays").resolve()
    assert settings.run_directory == (data_root / "runs").resolve()
    assert settings.map_asset_directory == (tmp_path / "managed-maps").resolve()
    assert settings.cache_directory == (tmp_path / "model-cache").resolve()
    assert settings.log_directory == (tmp_path / "application-logs").resolve()
    assert settings.engine_executable == engine.resolve()
    assert settings.engine_runtime_directory == engine_runtime.resolve()
    assert settings.ollama_url == "http://127.0.0.1:22434"
    assert settings.ollama_model == "test-model:latest"
    assert settings.watched_folders == (watch.resolve(),)
    assert settings.import_mode == "reference"
    assert settings.minimum_longitudinal_sample_size == 9
    assert settings.movement_sample_frames == 30
    assert settings.ffmpeg_executable == (tmp_path / "tools" / "ffmpeg.exe").resolve()
    assert settings.ffprobe_executable == (tmp_path / "tools" / "ffprobe.exe").resolve()
    assert settings.video_voice_provider == "windows_sapi"
    assert settings.video_voice_name == "Microsoft Zira Desktop"
    assert (settings.video_width, settings.video_height, settings.video_fps) == (1920, 1080, 60)
    assert settings.video_subtitle_mode == "burned"


def test_prefixed_environment_variables_override_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow product configuration without coupling it to CLI argument parsing."""
    data_root = tmp_path / "environment-data"
    monkeypatch.setenv("GENERALS_REPLAY_ANALYZER_DATA_ROOT", str(data_root))
    monkeypatch.setenv("GENERALS_REPLAY_ANALYZER_OLLAMA_MODEL", "environment-model:1")
    monkeypatch.setenv("GENERALS_REPLAY_ANALYZER_IMPORT_MODE", "reference")
    monkeypatch.setenv("GENERALS_REPLAY_ANALYZER_MINIMUM_LONGITUDINAL_SAMPLE_SIZE", "7")

    settings = AnalyzerSettings(_env_file=None)

    assert settings.data_root == data_root.resolve()
    assert settings.ollama_model == "environment-model:1"
    assert settings.import_mode == "reference"
    assert settings.minimum_longitudinal_sample_size == 7


def test_default_paths_are_derived_from_data_root(tmp_path: Path) -> None:
    """Give every later stage one stable product-owned location policy."""
    settings = _settings(tmp_path)

    assert settings.database_path == settings.data_root / "replay-analyzer.sqlite3"
    assert settings.managed_replay_directory == settings.data_root / "replays"
    assert settings.run_directory == settings.data_root / "runs"
    assert settings.map_asset_directory == settings.data_root / "map-assets-v1"
    assert settings.cache_directory == settings.data_root / "cache"
    assert settings.log_directory == settings.data_root / "logs"
    assert settings.video_run_directory == settings.data_root / "video-runs"


def test_video_settings_have_closed_safe_defaults_and_ranges(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert settings.ffmpeg_executable is None
    assert settings.ffprobe_executable is None
    assert settings.video_voice_provider == "windows_sapi"
    assert settings.video_voice_name == "Microsoft Zira Desktop"
    assert (settings.video_width, settings.video_height, settings.video_fps) == (1280, 720, 30)
    assert settings.video_subtitle_mode == "track"

    for changes in (
        {"video_width": 1279},
        {"video_height": 719},
        {"video_width": 639},
        {"video_height": 359},
        {"video_width": 7682},
        {"video_height": 4322},
        {"video_fps": 25},
        {"video_subtitle_mode": "none"},
        {"video_voice_provider": "network"},
        {"video_voice_name": ""},
    ):
        with pytest.raises(ValidationError):
            _settings(tmp_path, **changes)


def test_settings_construction_has_no_filesystem_side_effects(tmp_path: Path) -> None:
    """Keep configuration inspection separate from explicit runtime initialization."""
    data_root = tmp_path / "not-created"

    AnalyzerSettings(_env_file=None, data_root=data_root)

    assert not data_root.exists()


def test_ensure_directories_creates_only_owned_runtime_directories(tmp_path: Path) -> None:
    """Create product outputs explicitly without touching caller-owned inputs."""
    engine = tmp_path / "runtime" / "generalszh.exe"
    watched = tmp_path / "incoming"
    settings = _settings(
        tmp_path,
        database_path=tmp_path / "separate-database" / "library.sqlite3",
        engine_executable=engine,
        watched_folders=(watched,),
    )

    settings.ensure_directories()

    assert settings.data_root.is_dir()
    assert settings.database_path.parent.is_dir()
    assert settings.managed_replay_directory.is_dir()
    assert settings.run_directory.is_dir()
    assert settings.map_asset_directory.is_dir()
    assert settings.cache_directory.is_dir()
    assert settings.log_directory.is_dir()
    assert not settings.database_path.exists()
    assert not engine.exists()
    assert not watched.exists()


def test_runtime_output_inside_git_checkout_is_rejected() -> None:
    """Prevent runtime databases and assets from becoming repository artifacts."""
    with pytest.raises(ValidationError, match="Git checkout"):
        AnalyzerSettings(_env_file=None, data_root=PROJECT_ROOT / ".runtime-data")


def test_runtime_output_override_inside_git_checkout_is_rejected(tmp_path: Path) -> None:
    """Apply repository protection to individual output overrides as well as the main root."""
    with pytest.raises(ValidationError, match="database_path.*Git checkout"):
        _settings(tmp_path, database_path=PROJECT_ROOT / "runtime.sqlite3")


def test_repository_protection_cannot_be_disabled_through_model_input() -> None:
    """Keep the repository safety escape hatch outside the settings model surface."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AnalyzerSettings(
            _env_file=None,
            data_root=PROJECT_ROOT / ".test-runtime-data",
            allow_repository_data_root=True,
        )


def test_repository_protection_cannot_be_disabled_through_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevent a process environment setting from authorizing checkout writes."""
    monkeypatch.setenv(
        "GENERALS_REPLAY_ANALYZER_DATA_ROOT",
        str(PROJECT_ROOT / ".environment-runtime-data"),
    )
    monkeypatch.setenv("GENERALS_REPLAY_ANALYZER_ALLOW_REPOSITORY_DATA_ROOT", "true")

    with pytest.raises(ValidationError, match="ALLOW_REPOSITORY_DATA_ROOT"):
        AnalyzerSettings(_env_file=None)


def test_private_test_only_repository_override_is_explicit() -> None:
    """Permit checkout-local paths only through an unmistakably private test helper."""
    settings = AnalyzerSettings._for_testing_with_repository_outputs(
        _env_file=None,
        data_root=PROJECT_ROOT / ".test-runtime-data",
    )

    assert settings.data_root == (PROJECT_ROOT / ".test-runtime-data").resolve()
    assert not settings.data_root.exists()
    assert "allow_repository_data_root" not in AnalyzerSettings.model_fields


def test_unknown_prefixed_environment_variable_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject misspelled analyzer settings instead of silently using a default."""
    monkeypatch.setenv("GENERALS_REPLAY_ANALYZER_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("GENERALS_REPLAY_ANALYZER_OLLAMA_MODLE", "misspelled-model")

    with pytest.raises(ValidationError, match="OLLAMA_MODLE"):
        AnalyzerSettings(_env_file=None)


def test_unrelated_environment_variable_is_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep strict analyzer-prefix validation isolated from the host process environment."""
    monkeypatch.setenv("UNRELATED_APPLICATION_SETTING", "unrelated")

    settings = _settings(tmp_path)

    assert settings.data_root == (tmp_path / "product-data").resolve()


def test_run_directory_matches_accepted_runner_layout(tmp_path: Path) -> None:
    """Keep Task 9's fixed run transaction root non-overridable."""
    settings = _settings(tmp_path)

    assert settings.run_directory == settings.data_root / "runs"
    assert "run_directory" not in AnalyzerSettings.model_fields


def test_minimum_longitudinal_sample_size_must_be_positive(tmp_path: Path) -> None:
    """Reject a setting that would authorize zero-sample collection claims."""
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        _settings(tmp_path, minimum_longitudinal_sample_size=0)


def test_movement_sample_frames_has_the_accepted_default_and_range(tmp_path: Path) -> None:
    """Keep the effective telemetry interval aligned with the engine-run contract."""
    assert _settings(tmp_path).movement_sample_frames == 15

    for invalid in (0, 3601):
        with pytest.raises(ValidationError):
            _settings(tmp_path, movement_sample_frames=invalid)


def test_settings_are_frozen(tmp_path: Path) -> None:
    """Prevent path or model changes while a pipeline stage is using the settings."""
    settings = _settings(tmp_path)

    with pytest.raises(ValidationError, match="frozen"):
        settings.ollama_model = "changed"  # type: ignore[misc]


def test_fresh_runtime_configuration_activates_persisted_safe_settings(tmp_path: Path) -> None:
    """A restarted process must consume the exact safe values displayed by Settings."""

    configuration_root = tmp_path / "external-configuration"
    writer = ConfigurationStore(configuration_root=configuration_root, environment={})
    writer.apply(
        expected_revision=0,
        changes=(
            SettingChange("import_mode", "reference"),
            SettingChange("minimum_longitudinal_sample_size", 17),
            SettingChange("movement_sample_frames", 45),
            SettingChange("ollama_model", "qwen3.6:8b"),
            SettingChange("ollama_url", "http://[::1]:22434"),
        ),
    )

    runtime = load_runtime_configuration(
        configuration_root=configuration_root,
        environment={},
        values={"data_root": tmp_path / "product-data"},
    )

    assert runtime.settings.import_mode == "reference"
    assert runtime.settings.minimum_longitudinal_sample_size == 17
    assert runtime.settings.movement_sample_frames == 45
    assert runtime.settings.ollama_model == "qwen3.6:8b"
    assert runtime.settings.ollama_url == "http://[::1]:22434"
    assert runtime.snapshot == runtime.store.read()
    assert all(runtime.snapshot.source(key) == "persisted" for key, _value in runtime.snapshot.values)


def test_runtime_configuration_preserves_programmatic_then_environment_precedence(tmp_path: Path) -> None:
    """Explicit composition remains highest priority and both override kinds stay truthful/read-only."""

    configuration_root = tmp_path / "external-configuration"
    writer = ConfigurationStore(configuration_root=configuration_root, environment={})
    writer.apply(
        expected_revision=0,
        changes=(
            SettingChange("movement_sample_frames", 30),
            SettingChange("ollama_model", "persisted:1"),
        ),
    )
    runtime = load_runtime_configuration(
        configuration_root=configuration_root,
        environment={"GENERALS_REPLAY_ANALYZER_OLLAMA_MODEL": "environment:1"},
        values={
            "data_root": tmp_path / "product-data",
            "movement_sample_frames": 60,
        },
    )

    assert runtime.settings.movement_sample_frames == 60
    assert runtime.snapshot.source("movement_sample_frames") == "composition"
    assert runtime.settings.ollama_model == "environment:1"
    assert runtime.snapshot.source("ollama_model") == "environment"


def test_runtime_configuration_preserves_process_environment_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical loader must retain non-persisted BaseSettings environment inputs."""

    data_root = tmp_path / "environment-product-data"
    monkeypatch.setenv("GENERALS_REPLAY_ANALYZER_DATA_ROOT", str(data_root))

    runtime = load_runtime_configuration(
        configuration_root=tmp_path / "external-configuration",
    )

    assert runtime.settings.data_root == data_root.resolve()
