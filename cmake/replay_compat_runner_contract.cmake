# TheSuperHackers @feature Leex 24/08/2026 Lock the compatibility-runner shell to a modern-only, non-simulation CMake contract. (#TBD)
cmake_minimum_required(VERSION 3.25)

set(REPLAY_COMPAT_ROOT "${CMAKE_CURRENT_LIST_DIR}/..")

function(require_source_contains source_path expected description)
    file(READ "${source_path}" source_text)
    string(FIND "${source_text}" "${expected}" match_offset)
    if(match_offset EQUAL -1)
        message(FATAL_ERROR "Replay compatibility runner contract failed: ${description}")
    endif()
endfunction()

require_source_contains(
    "${REPLAY_COMPAT_ROOT}/CMakeLists.txt"
    "option(RTS_REPLAY_COMPAT_RUNNER"
    "the root must expose the opt-in compatibility-runner option")
require_source_contains(
    "${REPLAY_COMPAT_ROOT}/CMakeLists.txt"
    "add_compile_definitions(RTS_REPLAY_COMPAT_RUNNER=1)"
    "the modern option must define one ABI-consistent runner macro")
require_source_contains(
    "${REPLAY_COMPAT_ROOT}/CMakeLists.txt"
    "RTS_REPLAY_COMPAT_RUNNER AND NOT CMAKE_SIZEOF_VOID_P EQUAL 4"
    "the native compatibility runner must reject non-x86 build modes")
require_source_contains(
    "${REPLAY_COMPAT_ROOT}/GeneralsMD/Code/GameEngine/Include/Common/GlobalData.h"
    "m_autoCameraScriptPath"
    "the shared Zero Hour global data must hold the camera script path")
require_source_contains(
    "${REPLAY_COMPAT_ROOT}/GeneralsMD/Code/GameEngine/Include/Common/GlobalData.h"
    "m_recordVideoPath"
    "the shared Zero Hour global data must hold the video output path")
require_source_contains(
    "${REPLAY_COMPAT_ROOT}/GeneralsMD/Code/GameEngine/Source/Common/CommandLine.cpp"
    "{ \"-autocamera\", parseAutoCamera }"
    "the command line must accept an auto-camera script")
require_source_contains(
    "${REPLAY_COMPAT_ROOT}/GeneralsMD/Code/GameEngine/Source/Common/CommandLine.cpp"
    "{ \"-recordVideo\", parseRecordVideo }"
    "the command line must accept a capture output path")
require_source_contains(
    "${REPLAY_COMPAT_ROOT}/GeneralsMD/Code/GameEngine/Source/Common/CommandLine.cpp"
    "{ \"-videoRes\", parseVideoResolution }"
    "the command line must accept a capture resolution")
require_source_contains(
    "${REPLAY_COMPAT_ROOT}/GeneralsMD/Code/GameEngine/Source/Common/CommandLine.cpp"
    "{ \"-videoFps\", parseVideoFps }"
    "the command line must accept a capture frame rate")
require_source_contains(
    "${REPLAY_COMPAT_ROOT}/GeneralsMD/Code/GameEngine/Source/Common/CommandLine.cpp"
    "validateReplayCompatRunnerOptions();"
    "the command line must validate all runner options after startup parsing")
require_source_contains(
    "${REPLAY_COMPAT_ROOT}/GeneralsMD/Code/GameEngine/CMakeLists.txt"
    "Source/GameClient/AutoCameraDirector.cpp"
    "the runner must compile the Zero Hour camera director only when enabled")
require_source_contains(
    "${REPLAY_COMPAT_ROOT}/GeneralsMD/Code/GameEngineDevice/CMakeLists.txt"
    "\${CMAKE_SOURCE_DIR}/Core/GameEngineDevice/Source/W3DDevice/GameClient/W3DVideoWriter.cpp"
    "the Zero Hour runner must compile the native writer only when enabled")
require_source_contains(
    "${REPLAY_COMPAT_ROOT}/Core/GameEngineDevice/CMakeLists.txt"
    "add_library(corei_gameenginedevice_private INTERFACE)"
    "the shared device target must remain free of runner-only writer sources")
