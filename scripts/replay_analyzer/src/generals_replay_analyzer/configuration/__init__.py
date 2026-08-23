"""Revisioned application configuration with no web or database dependencies."""

from .store import (
    ConfigurationStore,
    EffectiveSettingsSnapshot,
    SettingChange,
    SettingsImpact,
    SettingsMutation,
    SettingsStoreError,
    normalize_ollama_endpoint,
    validate_ollama_model_name,
)

__all__ = (
    "ConfigurationStore",
    "EffectiveSettingsSnapshot",
    "SettingChange",
    "SettingsImpact",
    "SettingsMutation",
    "SettingsStoreError",
    "normalize_ollama_endpoint",
    "validate_ollama_model_name",
)
