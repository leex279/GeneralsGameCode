# z stands for Zero Hour, i stands for Interface
add_library(zi_gameengine_include INTERFACE)
add_library(zi_libraries_source_wwvegas INTERFACE)
add_library(zi_main INTERFACE)
add_library(zi_always INTERFACE)
add_library(zi_always_no_pch INTERFACE) # Use this for Shared Libs with MFC AFX

target_include_directories(zi_gameengine_include INTERFACE "GameEngine/Include")
target_include_directories(zi_libraries_source_wwvegas INTERFACE "Libraries/Source/WWVegas")
target_include_directories(zi_main INTERFACE "Main")

target_compile_definitions(zi_always INTERFACE
    RTS_ZEROHOUR=1
)
target_compile_definitions(zi_always_no_pch INTERFACE
    RTS_ZEROHOUR=1
)
option(RTS_BUILD_OPTION_GENERALS_ONLINE_REPLAY_COMPAT "Build modern Zero Hour with the opt-in Generals Online deterministic replay profile" OFF)
if(RTS_BUILD_OPTION_GENERALS_ONLINE_REPLAY_COMPAT AND NOT IS_VS6_BUILD)
    # TheSuperHackers @fix Leex 24/08/2026 Apply the opt-in Generals Online 60 Hz deterministic profile to modern Zero Hour replay playback. (#TBD)
    target_compile_definitions(zi_always INTERFACE
        GENERALS_ONLINE_HIGH_FPS_SERVER=1
        PRESERVE_SNIPING_EMPTY_STINGER_SITES=1
        RETAIL_COMPATIBLE_CRC=0
        RETAIL_COMPATIBLE_XFER_SAVE=0
        RETAIL_COMPATIBLE_PATHFINDING=0
        RETAIL_COMPATIBLE_PATHFINDING_ALLOCATION=0
        RETAIL_COMPATIBLE_AIGROUP=0
    )
    target_compile_definitions(zi_always_no_pch INTERFACE
        GENERALS_ONLINE_HIGH_FPS_SERVER=1
        PRESERVE_SNIPING_EMPTY_STINGER_SITES=1
        RETAIL_COMPATIBLE_CRC=0
        RETAIL_COMPATIBLE_XFER_SAVE=0
        RETAIL_COMPATIBLE_PATHFINDING=0
        RETAIL_COMPATIBLE_PATHFINDING_ALLOCATION=0
        RETAIL_COMPATIBLE_AIGROUP=0
    )
endif()
target_link_libraries(zi_gameengine_include INTERFACE
    corei_gameengine_include
)
target_link_libraries(zi_always INTERFACE
    corei_always # Must stay below so headers from game are included first
)
target_link_libraries(zi_always_no_pch INTERFACE
    corei_always_no_pch # Must stay below so headers from game are included first
)

# Contains internal libraries
add_subdirectory(Libraries)

# Base GameEngine library
add_subdirectory(GameEngine)

# Platform specific GameEngine code
add_subdirectory(GameEngineDevice)

# Contains the main game exe target.
add_subdirectory(Main)

if (RTS_BUILD_ZEROHOUR_TOOLS OR RTS_BUILD_ZEROHOUR_EXTRAS)
    add_subdirectory(Tools)
endif()
