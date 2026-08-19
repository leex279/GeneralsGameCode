"""Immutable metadata contracts extracted from Zero Hour replay headers."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ReplayFlags:
    """Replay outcome and per-slot disconnect flags stored in the fixed header."""

    desync_game: bool
    quit_early: bool
    player_disconnects: tuple[bool, ...]


@dataclass(frozen=True)
class ReplaySlot:
    """One explicit GameInfo slot, preserving its serialized slot position."""

    index: int
    kind: str
    name: str | None = None
    ip: int | None = None
    port: int | None = None
    accepted: bool | None = None
    has_map: bool | None = None
    color: int | None = None
    player_template: int | None = None
    start_position: int | None = None
    team: int | None = None
    nat_behavior: int | None = None
    ai_difficulty: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return JSON-ready slot fields without inventing values for non-player slots."""
        return {
            "index": self.index,
            "kind": self.kind,
            "name": self.name,
            "ip": self.ip,
            "port": self.port,
            "accepted": self.accepted,
            "has_map": self.has_map,
            "color": self.color,
            "player_template": self.player_template,
            "start_position": self.start_position,
            "team": self.team,
            "nat_behavior": self.nat_behavior,
            "ai_difficulty": self.ai_difficulty,
        }


@dataclass(frozen=True)
class ParseWarning:
    """Non-fatal source detail retained when a compatible extension token is ignored."""

    code: str
    message: str
    token: str

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-ready warning record."""
        return {"code": self.code, "message": self.message, "token": self.token}


@dataclass(frozen=True)
class ReplayHeader:
    """All fields consumed by RecorderClass::readReplayHeader, ending before commands."""

    magic: str
    start_time: int
    end_time: int
    frame_count: int
    flags: ReplayFlags
    replay_name: str
    system_time: tuple[int, int, int, int, int, int, int, int]
    version_string: str
    version_time_string: str
    version_number: int
    exe_crc: int
    ini_crc: int
    game_options: str
    local_player_index: int
    header_end_offset: int
    map: str
    map_contents_mask: int
    map_crc: int
    map_size: int
    seed: int
    crc_interval: int
    use_stats: int | None
    superweapon_restriction: int | None
    starting_cash: int | None
    old_factions_only: bool | None
    slots: tuple[ReplaySlot, ...]
    warnings: tuple[ParseWarning, ...]

    def to_dict(self) -> dict[str, object]:
        """Return the stable expected-fixture representation for this parsed header."""
        return {
            "magic": self.magic,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "frame_count": self.frame_count,
            "flags": {
                "desync_game": self.flags.desync_game,
                "quit_early": self.flags.quit_early,
                "player_disconnects": list(self.flags.player_disconnects),
            },
            "replay_name": self.replay_name,
            "system_time": {
                "year": self.system_time[0],
                "month": self.system_time[1],
                "day_of_week": self.system_time[2],
                "day": self.system_time[3],
                "hour": self.system_time[4],
                "minute": self.system_time[5],
                "second": self.system_time[6],
                "milliseconds": self.system_time[7],
            },
            "version_string": self.version_string,
            "version_time_string": self.version_time_string,
            "version_number": self.version_number,
            "exe_crc": self.exe_crc,
            "ini_crc": self.ini_crc,
            "game_options": self.game_options,
            "local_player_index": self.local_player_index,
            "header_end_offset": self.header_end_offset,
            "map": self.map,
            "map_contents_mask": self.map_contents_mask,
            "map_crc": self.map_crc,
            "map_size": self.map_size,
            "seed": self.seed,
            "crc_interval": self.crc_interval,
            "use_stats": self.use_stats,
            "superweapon_restriction": self.superweapon_restriction,
            "starting_cash": self.starting_cash,
            "old_factions_only": self.old_factions_only,
            "slots": [slot.to_dict() for slot in self.slots],
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


# TheSuperHackers @feature Leex 19/08/2026 Preserve Recorder setup fields between the replay header and command stream. (#TBD)
@dataclass(frozen=True)
class ReplaySetup:
    """Four source-recorded Int values immediately following ``readReplayHeader`` output."""

    difficulty: int
    original_game_mode: int
    rank_points: int
    max_fps: int
    start_offset: int
    end_offset: int

    def to_dict(self) -> dict[str, int]:
        """Return observed setup values and their exact evidence boundaries for JSON inspection."""
        return {
            "difficulty": self.difficulty,
            "original_game_mode": self.original_game_mode,
            "rank_points": self.rank_points,
            "max_fps": self.max_fps,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
        }
