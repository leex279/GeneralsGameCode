# Copyright 2026 TheSuperHackers
#
# Binary parser for C&C Generals & Zero Hour .rep replay files.

import struct
import os
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple
from .constants import GameMessageType, ArgumentDataType, FACTION_NAMES, COLOR_NAMES

@dataclass
class PlayerSlot:
    slot_id: int
    name: str
    is_human: bool
    is_ai: bool
    ai_difficulty: Optional[str]
    ip_hex: str
    port: int
    color_id: int
    color_name: str
    faction_id: int
    faction_name: str
    start_pos: int
    team: int
    is_accepted: bool

@dataclass
class CommandArgument:
    arg_type: ArgumentDataType
    type_name: str
    value: Any

@dataclass
class GameCommand:
    frame: int
    timestamp_sec: float
    command_type: int
    command_name: str
    player_index: int
    args: List[CommandArgument] = field(default_factory=list)

@dataclass
class ReplayMetadata:
    filename: str
    start_time: int
    end_time: int
    frame_count: int
    duration_seconds: float
    fps: float
    desync_occurred: bool
    quit_early: bool
    player_disconnects: List[bool]
    replay_name: str
    version_string: str
    build_time: str
    version_number: int
    exe_crc: int
    ini_crc: int
    map_name: str
    starting_cash: int
    seed: int
    local_player_index: int
    players: List[PlayerSlot] = field(default_factory=list)

@dataclass
class ParsedReplay:
    metadata: ReplayMetadata
    commands: List[GameCommand] = field(default_factory=list)


class ReplayParser:
    """Parses C&C Generals / Zero Hour replay files (.rep)."""

    def __init__(self, filepath: str, logic_fps: float = 30.0):
        self.filepath = filepath
        self.logic_fps = logic_fps

    def _read_null_terminated_utf16(self, f) -> str:
        chars = []
        while True:
            chunk = f.read(2)
            if not chunk or chunk == b'\x00\x00':
                break
            chars.append(chunk.decode('utf-16-le', errors='replace'))
        return ''.join(chars)

    def _read_null_terminated_ascii(self, f) -> str:
        chars = []
        while True:
            chunk = f.read(1)
            if not chunk or chunk == b'\x00':
                break
            chars.append(chunk.decode('latin-1', errors='replace'))
        return ''.join(chars)

    def _parse_game_options(self, options_str: str) -> Tuple[Dict[str, str], List[PlayerSlot]]:
        data = {}
        slots: List[PlayerSlot] = []
        parts = options_str.split(';')
        
        for p in parts:
            if not p or '=' not in p:
                continue
            key, val = p.split('=', 1)
            data[key] = val
            
            if key == 'S':
                # Slot string format: H<Name>,<IP>,<Port>,<Accept&Map>,<Color>,<Faction>,<StartPos>,<Team>,<NAT>
                # or C<Difficulty>,... for AI
                raw_slots = val.split(':')
                slot_id = 0
                for s in raw_slots:
                    if not s or s == 'X':
                        slot_id += 1
                        continue
                    
                    fields = s.split(',')
                    if len(fields) < 8:
                        slot_id += 1
                        continue
                    
                    prefix = fields[0][0] if len(fields[0]) > 0 else 'H'
                    raw_name = fields[0][1:] if len(fields[0]) > 1 else "Player"
                    is_human = (prefix == 'H')
                    is_ai = (prefix == 'C')
                    ai_diff = None
                    if is_ai:
                        if len(fields[0]) > 1:
                            ai_code = fields[0][1]
                            ai_diff = {"E": "Easy", "M": "Medium", "H": "Hard"}.get(ai_code, "Medium")
                    
                    ip_hex = fields[1] if len(fields) > 1 else "0"
                    try:
                        port = int(fields[2]) if len(fields) > 2 else 8088
                    except ValueError:
                        port = 8088
                        
                    acc_map = fields[3] if len(fields) > 3 else "TT"
                    is_accepted = acc_map.startswith("T")
                    
                    try:
                        color_id = int(fields[4]) if len(fields) > 4 else -1
                    except ValueError:
                        color_id = -1
                        
                    try:
                        faction_id = int(fields[5]) if len(fields) > 5 else -1
                    except ValueError:
                        faction_id = -1
                        
                    try:
                        start_pos = int(fields[6]) if len(fields) > 6 else -1
                    except ValueError:
                        start_pos = -1
                        
                    try:
                        team = int(fields[7]) if len(fields) > 7 else -1
                    except ValueError:
                        team = -1
                    
                    slot = PlayerSlot(
                        slot_id=slot_id,
                        name=raw_name,
                        is_human=is_human,
                        is_ai=is_ai,
                        ai_difficulty=ai_diff,
                        ip_hex=ip_hex,
                        port=port,
                        color_id=color_id,
                        color_name=COLOR_NAMES.get(color_id, f"Color {color_id}"),
                        faction_id=faction_id,
                        faction_name=FACTION_NAMES.get(faction_id, f"Faction {faction_id}"),
                        start_pos=start_pos,
                        team=team,
                        is_accepted=is_accepted
                    )
                    slots.append(slot)
                    slot_id += 1
                    
        return data, slots

    def parse(self, parse_commands: bool = True) -> ParsedReplay:
        if not os.path.exists(self.filepath):
            raise FileNotFoundError(f"Replay file not found: {self.filepath}")

        with open(self.filepath, 'rb') as f:
            magic = f.read(6)
            if magic != b'GENREP':
                raise ValueError(f"Invalid replay magic header: {magic!r}. Expected b'GENREP'.")

            start_time, end_time, frame_count = struct.unpack('<III', f.read(12))
            desync_game, quit_early = struct.unpack('<? ?', f.read(2))
            player_discons = list(struct.unpack('<8?', f.read(8)))

            replay_name = self._read_null_terminated_utf16(f)
            f.read(16) # SYSTEMTIME

            version_string = self._read_null_terminated_utf16(f)
            build_time = self._read_null_terminated_utf16(f)
            ver_num, exe_crc, ini_crc = struct.unpack('<III', f.read(12))

            game_options_str = self._read_null_terminated_ascii(f)
            local_idx_str = self._read_null_terminated_ascii(f)
            try:
                local_player_idx = int(local_idx_str)
            except ValueError:
                local_player_idx = -1

            diff, orig_mode, rank_points, max_fps = struct.unpack('<iiii', f.read(16))

            opts_dict, players = self._parse_game_options(game_options_str)
            raw_map_name = opts_dict.get('M', 'Unknown Map')
            # Clean map name
            if raw_map_name.startswith('4buserdata/maps/'):
                map_name = raw_map_name[len('4buserdata/maps/'):]
            elif raw_map_name.startswith('03maps/'):
                map_name = raw_map_name[len('03maps/'):]
            else:
                map_name = raw_map_name

            try:
                starting_cash = int(opts_dict.get('SC', 10000))
            except ValueError:
                starting_cash = 10000

            try:
                seed = int(opts_dict.get('SD', 0))
            except ValueError:
                seed = 0

            duration_seconds = frame_count / self.logic_fps

            metadata = ReplayMetadata(
                filename=os.path.basename(self.filepath),
                start_time=start_time,
                end_time=end_time,
                frame_count=frame_count,
                duration_seconds=duration_seconds,
                fps=self.logic_fps,
                desync_occurred=desync_game,
                quit_early=quit_early,
                player_disconnects=player_discons,
                replay_name=replay_name,
                version_string=version_string,
                build_time=build_time,
                version_number=ver_num,
                exe_crc=exe_crc,
                ini_crc=ini_crc,
                map_name=map_name,
                starting_cash=starting_cash,
                seed=seed,
                local_player_index=local_player_idx,
                players=players
            )

            commands: List[GameCommand] = []

            if not parse_commands:
                return ParsedReplay(metadata=metadata, commands=[])

            # Parse command stream
            while True:
                frame_bytes = f.read(4)
                if len(frame_bytes) < 4:
                    break
                cmd_frame, = struct.unpack('<I', frame_bytes)
                type_bytes = f.read(4)
                if len(type_bytes) < 4:
                    break
                cmd_type, = struct.unpack('<i', type_bytes)
                p_idx_bytes = f.read(4)
                if len(p_idx_bytes) < 4:
                    break
                player_idx, = struct.unpack('<i', p_idx_bytes)

                num_types_byte = f.read(1)
                if len(num_types_byte) < 1:
                    break
                num_types, = struct.unpack('<B', num_types_byte)

                type_specs = []
                for _ in range(num_types):
                    spec = f.read(2)
                    if len(spec) < 2:
                        break
                    t, count = struct.unpack('<BB', spec)
                    type_specs.append((t, count))

                parsed_args: List[CommandArgument] = []
                for t, count in type_specs:
                    for _ in range(count):
                        arg_data_type = ArgumentDataType(t) if t in ArgumentDataType._value2member_map_ else ArgumentDataType.UNKNOWN
                        arg_val = None
                        if t == ArgumentDataType.INTEGER:
                            v = f.read(4)
                            if len(v) == 4: arg_val = struct.unpack('<i', v)[0]
                        elif t == ArgumentDataType.REAL:
                            v = f.read(4)
                            if len(v) == 4: arg_val = struct.unpack('<f', v)[0]
                        elif t == ArgumentDataType.BOOLEAN:
                            v = f.read(1)
                            if len(v) == 1: arg_val = struct.unpack('<?', v)[0]
                        elif t in (ArgumentDataType.OBJECT_ID, ArgumentDataType.DRAWABLE_ID, ArgumentDataType.TEAM_ID, ArgumentDataType.TIMESTAMP):
                            v = f.read(4)
                            if len(v) == 4: arg_val = struct.unpack('<I', v)[0]
                        elif t == ArgumentDataType.LOCATION:
                            v = f.read(12)
                            if len(v) == 12:
                                x, y, z = struct.unpack('<fff', v)
                                arg_val = {"x": round(x, 2), "y": round(y, 2), "z": round(z, 2)}
                        elif t == ArgumentDataType.PIXEL:
                            v = f.read(8)
                            if len(v) == 8:
                                x, y = struct.unpack('<ii', v)
                                arg_val = {"x": x, "y": y}
                        elif t == ArgumentDataType.PIXEL_REGION:
                            v = f.read(16)
                            if len(v) == 16:
                                x, y, w, h = struct.unpack('<iiii', v)
                                arg_val = {"x": x, "y": y, "width": w, "height": h}
                        elif t == ArgumentDataType.WIDE_CHAR:
                            v = f.read(2)
                            if len(v) == 2: arg_val = v.decode('utf-16-le', errors='replace')
                        else:
                            f.read(4)

                        parsed_args.append(CommandArgument(
                            arg_type=arg_data_type,
                            type_name=arg_data_type.name,
                            value=arg_val
                        ))

                try:
                    cmd_enum = GameMessageType(cmd_type)
                    cmd_name = cmd_enum.name
                except ValueError:
                    cmd_name = f"CMD_{cmd_type}"

                cmd = GameCommand(
                    frame=cmd_frame,
                    timestamp_sec=round(cmd_frame / self.logic_fps, 2),
                    command_type=cmd_type,
                    command_name=cmd_name,
                    player_index=player_idx,
                    args=parsed_args
                )
                commands.append(cmd)

            return ParsedReplay(metadata=metadata, commands=commands)
