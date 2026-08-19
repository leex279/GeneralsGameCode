# Copyright 2026 TheSuperHackers
#
# GeneralsGameCode Replay Analyzer Package.

from importlib import import_module

# TheSuperHackers @refactor Leex 19/08/2026 Defer legacy imports until their export is used. (#TBD)
_LAZY_EXPORTS = {
    "ReplayParser": (".parser", "ReplayParser"),
    "ParsedReplay": (".parser", "ParsedReplay"),
    "GameCommand": (".parser", "GameCommand"),
    "ReplayMetadata": (".parser", "ReplayMetadata"),
    "PlayerSlot": (".parser", "PlayerSlot"),
    "MetricsCalculator": (".metrics", "MetricsCalculator"),
    "MatchMetrics": (".metrics", "MatchMetrics"),
    "PlayerMetrics": (".metrics", "PlayerMetrics"),
    "TimelineEvent": (".metrics", "TimelineEvent"),
    "StrategyAnalyzer": (".heuristics", "StrategyAnalyzer"),
    "ReplayReporter": (".reporter", "ReplayReporter"),
    "GameMessageType": (".constants", "GameMessageType"),
    "ArgumentDataType": (".constants", "ArgumentDataType"),
    "FACTION_NAMES": (".constants", "FACTION_NAMES"),
    "COLOR_NAMES": (".constants", "COLOR_NAMES"),
}

__all__ = [
    "ReplayParser",
    "ParsedReplay",
    "GameCommand",
    "ReplayMetadata",
    "PlayerSlot",
    "MetricsCalculator",
    "MatchMetrics",
    "PlayerMetrics",
    "TimelineEvent",
    "StrategyAnalyzer",
    "ReplayReporter",
    "GameMessageType",
    "ArgumentDataType",
    "FACTION_NAMES",
    "COLOR_NAMES",
]


def __getattr__(name):
    try:
        module_name, export_name = _LAZY_EXPORTS[name]
    except KeyError:
        raise AttributeError("module {!r} has no attribute {!r}".format(__name__, name))

    export = getattr(import_module(module_name, __name__), export_name)
    globals()[name] = export
    return export
