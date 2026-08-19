# Copyright 2026 TheSuperHackers
#
# Part of GeneralsGameCode Replay Analyzer tool.

from enum import IntEnum

class GameMessageType(IntEnum):
    MSG_INVALID = 0
    MSG_FRAME_TICK = 1

    # Raw mouse & keyboard
    MSG_RAW_MOUSE_BEGIN = 2
    MSG_RAW_MOUSE_POSITION = 3
    MSG_RAW_MOUSE_LEFT_BUTTON_DOWN = 4
    MSG_RAW_MOUSE_LEFT_DOUBLE_CLICK = 5
    MSG_RAW_MOUSE_LEFT_BUTTON_UP = 6
    MSG_RAW_MOUSE_LEFT_CLICK = 7
    MSG_RAW_MOUSE_LEFT_DRAG = 8
    MSG_RAW_MOUSE_MIDDLE_BUTTON_DOWN = 9
    MSG_RAW_MOUSE_MIDDLE_DOUBLE_CLICK = 10
    MSG_RAW_MOUSE_MIDDLE_BUTTON_UP = 11
    MSG_RAW_MOUSE_MIDDLE_DRAG = 12
    MSG_RAW_MOUSE_RIGHT_BUTTON_DOWN = 13
    MSG_RAW_MOUSE_RIGHT_DOUBLE_CLICK = 14
    MSG_RAW_MOUSE_RIGHT_BUTTON_UP = 15
    MSG_RAW_MOUSE_RIGHT_DRAG = 16
    MSG_RAW_MOUSE_WHEEL = 17
    MSG_RAW_MOUSE_END = 18

    MSG_RAW_KEY_DOWN = 19
    MSG_RAW_KEY_UP = 20

    MSG_MOUSE_LEFT_CLICK = 21
    MSG_MOUSE_LEFT_DOUBLE_CLICK = 22
    MSG_MOUSE_MIDDLE_CLICK = 23
    MSG_MOUSE_MIDDLE_DOUBLE_CLICK = 24
    MSG_MOUSE_RIGHT_CLICK = 25
    MSG_MOUSE_RIGHT_DOUBLE_CLICK = 26

    MSG_CLEAR_GAME_DATA = 150
    MSG_NEW_GAME = 151

    # Network Messages
    MSG_BEGIN_NETWORK_MESSAGES = 1000
    MSG_CREATE_SELECTED_GROUP = 1001
    MSG_CREATE_SELECTED_GROUP_NO_SOUND = 1002
    MSG_DESTROY_SELECTED_GROUP = 1003
    MSG_REMOVE_FROM_SELECTED_GROUP = 1004
    MSG_SELECTED_GROUP_COMMAND = 1005

    # Teams / Hotkey Squads
    MSG_CREATE_TEAM0 = 1006
    MSG_CREATE_TEAM1 = 1007
    MSG_CREATE_TEAM2 = 1008
    MSG_CREATE_TEAM3 = 1009
    MSG_CREATE_TEAM4 = 1010
    MSG_CREATE_TEAM5 = 1011
    MSG_CREATE_TEAM6 = 1012
    MSG_CREATE_TEAM7 = 1013
    MSG_CREATE_TEAM8 = 1014
    MSG_CREATE_TEAM9 = 1015

    MSG_SELECT_TEAM0 = 1016
    MSG_SELECT_TEAM1 = 1017
    MSG_SELECT_TEAM2 = 1018
    MSG_SELECT_TEAM3 = 1019
    MSG_SELECT_TEAM4 = 1020
    MSG_SELECT_TEAM5 = 1021
    MSG_SELECT_TEAM6 = 1022
    MSG_SELECT_TEAM7 = 1023
    MSG_SELECT_TEAM8 = 1024
    MSG_SELECT_TEAM9 = 1025

    MSG_ADD_TEAM0 = 1026
    MSG_ADD_TEAM1 = 1027
    MSG_ADD_TEAM2 = 1028
    MSG_ADD_TEAM3 = 1029
    MSG_ADD_TEAM4 = 1030
    MSG_ADD_TEAM5 = 1031
    MSG_ADD_TEAM6 = 1032
    MSG_ADD_TEAM7 = 1033
    MSG_ADD_TEAM8 = 1034
    MSG_ADD_TEAM9 = 1035

    MSG_DO_ATTACKSQUAD = 1036
    MSG_DO_WEAPON = 1037
    MSG_DO_WEAPON_AT_LOCATION = 1038
    MSG_DO_WEAPON_AT_OBJECT = 1039
    MSG_DO_SPECIAL_POWER = 1040
    MSG_DO_SPECIAL_POWER_AT_LOCATION = 1041
    MSG_DO_SPECIAL_POWER_AT_OBJECT = 1042
    MSG_SET_RALLY_POINT = 1043
    MSG_PURCHASE_SCIENCE = 1044
    MSG_QUEUE_UPGRADE = 1045
    MSG_CANCEL_UPGRADE = 1046
    MSG_QUEUE_UNIT_CREATE = 1047
    MSG_CANCEL_UNIT_CREATE = 1048
    MSG_DOZER_CONSTRUCT = 1049
    MSG_DOZER_CONSTRUCT_LINE = 1050
    MSG_DOZER_CANCEL_CONSTRUCT = 1051
    MSG_SELL = 1052
    MSG_EXIT = 1053
    MSG_EVACUATE = 1054
    MSG_EXECUTE_RAILED_TRANSPORT = 1055
    MSG_COMBATDROP_AT_LOCATION = 1056
    MSG_COMBATDROP_AT_OBJECT = 1057
    MSG_AREA_SELECTION_DEPRECATED = 1058
    MSG_DO_ATTACK_OBJECT = 1059
    MSG_DO_FORCE_ATTACK_OBJECT = 1060
    MSG_DO_FORCE_ATTACK_GROUND = 1061
    MSG_GET_REPAIRED = 1062
    MSG_GET_HEALED = 1063
    MSG_DO_REPAIR = 1064
    MSG_RESUME_CONSTRUCTION = 1065
    MSG_ENTER = 1066
    MSG_DOCK = 1067
    MSG_DO_MOVETO = 1068
    MSG_DO_ATTACKMOVETO = 1069
    MSG_DO_FORCEMOVETO = 1070
    MSG_ADD_WAYPOINT = 1071
    MSG_DO_GUARD_POSITION = 1072
    MSG_DO_GUARD_OBJECT = 1073
    MSG_DO_STOP = 1074
    MSG_DO_SCATTER = 1075
    MSG_INTERNET_HACK = 1076
    MSG_DO_CHEER = 1077
    MSG_TOGGLE_OVERCHARGE = 1078
    MSG_SWITCH_WEAPONS = 1079
    MSG_CONVERT_TO_CARBOMB = 1080
    MSG_CAPTUREBUILDING = 1081
    MSG_DISABLEVEHICLE_HACK = 1082
    MSG_STEALCASH_HACK = 1083
    MSG_DISABLEBUILDING_HACK = 1084
    MSG_SNIPE_VEHICLE = 1085
    MSG_DO_SPECIAL_POWER_OVERRIDE_DESTINATION = 1086
    MSG_DO_SALVAGE = 1087
    MSG_CLEAR_INGAME_POPUP_MESSAGE = 1088
    MSG_PLACE_BEACON = 1089
    MSG_REMOVE_BEACON = 1090
    MSG_SET_BEACON_TEXT = 1091
    MSG_SET_REPLAY_CAMERA = 1092
    MSG_SELF_DESTRUCT = 1093
    MSG_CREATE_FORMATION = 1094
    MSG_LOGIC_CRC = 1095
    MSG_SET_MINE_CLEARING_DETAIL = 1096
    MSG_ENABLE_RETALIATION_MODE = 1097

    MSG_END_NETWORK_MESSAGES = 1999

class ArgumentDataType(IntEnum):
    INTEGER = 0
    REAL = 1
    BOOLEAN = 2
    OBJECT_ID = 3
    DRAWABLE_ID = 4
    TEAM_ID = 5
    LOCATION = 6
    PIXEL = 7
    PIXEL_REGION = 8
    TIMESTAMP = 9
    WIDE_CHAR = 10
    UNKNOWN = 11

FACTION_NAMES = {
    -2: "Observer",
    -1: "Random",
    0: "USA",
    1: "USA Air Force (Granger)",
    2: "USA Laser (Townes)",
    3: "USA Superweapon (Alexander)",
    4: "China",
    5: "China Tank (Kwai)",
    6: "China Infantry (Fai)",
    7: "China Nuke (Tao)",
    8: "GLA",
    9: "GLA Toxin (Thrax)",
    10: "GLA Demolition (Juhziz)",
    11: "GLA Stealth (Kassad)",
    12: "Boss (Leang)"
}

COLOR_NAMES = {
    -1: "Random",
    0: "Red",
    1: "Blue",
    2: "Green",
    3: "Yellow",
    4: "Cyan",
    5: "Orange",
    6: "Purple",
    7: "Magenta"
}

# Known common template / unit / structure IDs in Zero Hour
ENTITY_NAMES = {
    # USA Structures & Units
    1229: "USA Cold Fusion Reactor",
    1254: "USA Supply Center",
    1250: "USA Barracks",
    1265: "USA War Factory",
    1260: "USA Patriot Battery",
    1290: "USA Supply Drop Zone",
    40: "USA Cold Fusion Reactor",
    45: "USA Supply Center",
    48: "USA Barracks",
    49: "USA War Factory",
    43: "USA Patriot Battery",
    135: "USA Construction Dozer",
    66: "USA Chinook Supply Heli",
    106: "USA Ranger / Missile Defender",
    127: "USA Humvee",
    129: "USA Ambulance / Crusader Tank",
    34: "USA Construction Dozer",
    52: "USA Ranger",
    
    # China Structures & Units
    1253: "China Supply Center",
    1264: "China War Factory",
    1259: "China Power Plant",
    1234: "China Barracks",
    1282: "China Propaganda Tower",
    1269: "China Bunker / Gatling Cannon",
    285: "China Construction Dozer",
    262: "China Red Guard / Tank Hunter",
    284: "China Battlemaster / Gatling Tank",
    
    # GLA Structures & Units
    1996: "GLA Supply Stash",
    1997: "GLA Barracks",
    1998: "GLA Arms Dealer",
    2000: "GLA Tunnel Network",
    1889: "GLA Supply Stash",
    1885: "GLA Barracks",
    1883: "GLA Tunnel Network",
    1887: "GLA Arms Dealer",
    1882: "GLA Black Market",
    1774: "GLA Supply Stash",
    1776: "GLA Barracks",
    1775: "GLA Tunnel Network",
    1777: "GLA Arms Dealer",
    1771: "GLA Palace",
    1991: "GLA Worker",
    1993: "GLA Rebel / RPG Trooper",
    1990: "GLA Rebel",
    1989: "GLA Technical / Scorpion Tank",
    1987: "GLA Quad Cannon / Marauder",
    1873: "GLA Worker",
    1861: "GLA Rebel / RPG Trooper",
    1858: "GLA Technical",
    1767: "GLA Worker",
    1759: "GLA Rebel / Technical"
}

