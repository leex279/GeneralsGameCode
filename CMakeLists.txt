cmake_minimum_required(VERSION 3.25)

# Use packagename_ROOT for FindPackage.
if(POLICY CMP0074)
    cmake_policy(SET CMP0074 NEW)
endif()

# Disable default MSVC setting CRT type so we can set it ourselves.
if(POLICY CMP0091)
    cmake_policy(SET CMP0091 NEW)
endif()

# Disable default MSVC warning level so we can set it ourselves.
if(POLICY CMP0092)
    cmake_policy(SET CMP0092 NEW)
endif()

# Allow specifying MSVC debug configurations.
#if(POLICY CMP0141)
#    cmake_policy(SET CMP0141 NEW)
#endif()

set(CMAKE_MODULE_PATH ${CMAKE_MODULE_PATH} "${CMAKE_SOURCE_DIR}/cmake/")

include(FeatureSummary)
include(CMakeDependentOption)

# We don't support in tree builds, so help people make the right choice.
if (CMAKE_BINARY_DIR STREQUAL CMAKE_SOURCE_DIR)
    message(FATAL_ERROR "Building in-source is not supported! Create a build dir and remove ${CMAKE_SOURCE_DIR}/CMakeCache.txt")
endif()

# Top level project, doesn't really affect anything.
project(genzh LANGUAGES C CXX)

# TheSuperHackers @feature Leex 24/08/2026 Gate the replay compatibility runner without changing the pinned Generals Online simulation profile. (#TBD)
option(RTS_REPLAY_COMPAT_RUNNER "Enable modern-only replay capture and camera runner plumbing" OFF)

# TheSuperHackers @build Leex 24/08/2026 Reject unsupported pointer widths before the Direct3D 8 compatibility runner configures. (#TBD)
if(RTS_REPLAY_COMPAT_RUNNER AND NOT CMAKE_SIZEOF_VOID_P EQUAL 4)
    message(FATAL_ERROR "RTS_REPLAY_COMPAT_RUNNER requires a 32-bit build")
endif()

# This file handles extra settings wanted/needed for different compilers.
include(cmake/compilers.cmake)

if(RTS_REPLAY_COMPAT_RUNNER AND NOT IS_VS6_BUILD)
    # TheSuperHackers @feature Leex 24/08/2026 Define the runner process-wide so GlobalData keeps one ABI in engine and device targets. (#TBD)
    add_compile_definitions(RTS_REPLAY_COMPAT_RUNNER=1)
endif()

# Debug symbol stripping for Release builds (MinGW)
include(cmake/debug_strip.cmake)

include(FetchContent)

# MinGW-w64 specific configuration
if(MINGW)
    include(cmake/mingw.cmake)
    include(cmake/reactos-atl.cmake)
    include(cmake/widl.cmake)
endif()

# Find/Add build dependencies and stubs shared by all projects
if((WIN32 OR "${CMAKE_SYSTEM}" MATCHES "Windows") AND ${CMAKE_SIZEOF_VOID_P} EQUAL 4)
    include(cmake/miles.cmake)
    include(cmake/bink.cmake)
    include(cmake/dx8.cmake)
endif()

# Define a dummy stlport target when not on VC6.
if (IS_VS6_BUILD)
    include(cmake/stlport.cmake)
else()
    add_library(stlport INTERFACE)
endif()

include(cmake/config.cmake)
include(cmake/gamespy.cmake)
include(cmake/lzhl.cmake)

if (IS_VS6_BUILD)
    # The original max sdk does not compile against a modern compiler.
    # If there is a desire to make this work, then a fixed max sdk needs to be created.
    add_subdirectory(Dependencies/MaxSDK)
endif()
add_subdirectory(Dependencies/Utility)

# To be removed when abandoning VC6
if (IS_VS6_BUILD)
    include_directories(Dependencies/Utility)
    add_compile_options(/FIUtility/CppMacros.h)
endif()

add_subdirectory(resources)

add_subdirectory(Core)

# Add main build targets
if(RTS_BUILD_ZEROHOUR)
    add_subdirectory(GeneralsMD)
endif()

if(RTS_BUILD_GENERALS)
    add_subdirectory(Generals)
endif()

feature_summary(WHAT ENABLED_FEATURES DESCRIPTION "Enabled features:")
feature_summary(WHAT DISABLED_FEATURES DESCRIPTION "Disabled features:")
