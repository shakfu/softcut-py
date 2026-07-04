cmake_minimum_required(VERSION 3.15...3.31)
project(${SKBUILD_PROJECT_NAME} VERSION ${SKBUILD_PROJECT_VERSION} LANGUAGES CXX C)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

find_package(Python REQUIRED COMPONENTS Interpreter Development.Module)
find_package(nanobind CONFIG REQUIRED)

# Optional (EXPERIMENTAL) native OSC transport: compile the vendored tinyosc C
# wire codec into the extension for a dependency-free OSC server. OFF by default
# and not enabled in the published wheels; the pure-Python option (python-osc)
# is the default. Enable in a source build with:
#   SKBUILD_CMAKE_DEFINE="SOFTCUT_ENABLE_TINYOSC=ON" (see `make build-tinyosc`).
option(SOFTCUT_ENABLE_TINYOSC
    "Compile the experimental vendored tinyosc native OSC transport into _core" OFF)

# softcut-lib: the per-voice DSP engine we are wrapping. It owns no buffer
# memory and has no external dependencies, so we compile its sources straight
# into the extension module.
set(SOFTCUT_LIB ${CMAKE_CURRENT_SOURCE_DIR}/thirdparty/softcut-lib/softcut-lib)

# miniaudio: single-header realtime audio device backend (CoreAudio/ALSA/WASAPI).
# miniaudio.c is the one translation unit that defines MINIAUDIO_IMPLEMENTATION.
set(MINIAUDIO ${CMAKE_CURRENT_SOURCE_DIR}/thirdparty/miniaudio)

# tinyosc: single-file OSC 1.0 wire codec (parse/serialize only, no sockets).
# The UDP transport/receive thread is our own C++ glue in _core.cpp.
set(TINYOSC ${CMAKE_CURRENT_SOURCE_DIR}/thirdparty/tinyosc)

nanobind_add_module(_core
    src/softcut/_core.cpp
    ${MINIAUDIO}/miniaudio.c
    ${SOFTCUT_LIB}/src/Voice.cpp
    ${SOFTCUT_LIB}/src/ReadWriteHead.cpp
    ${SOFTCUT_LIB}/src/SubHead.cpp
    ${SOFTCUT_LIB}/src/FadeCurves.cpp
    ${SOFTCUT_LIB}/src/Svf.cpp)

target_include_directories(_core PRIVATE
    ${SOFTCUT_LIB}/include
    ${SOFTCUT_LIB}/src
    ${MINIAUDIO})

# We only need realtime device I/O, not miniaudio's file decoders/encoders.
target_compile_definitions(_core PRIVATE MA_NO_DECODING MA_NO_ENCODING)

if(MSVC)
    target_compile_options(_core PRIVATE /W4)
    # MSVC's <cmath> only defines M_PI/M_PI_2 (used by the vendored DSP sources)
    # when _USE_MATH_DEFINES is set before the header is included.
    target_compile_definitions(_core PRIVATE _USE_MATH_DEFINES)
else()
    target_compile_options(_core PRIVATE -Wall -Wextra)
endif()

# Platform audio backends required by miniaudio. miniaudio runtime-links the
# backend libraries (ALSA/PulseAudio/JACK, WASAPI/DirectSound/WinMM) via dlopen/
# LoadLibrary, so no backend dev packages are needed at build time.
if(APPLE)
    target_link_libraries(_core PRIVATE
        "-framework CoreFoundation"
        "-framework CoreAudio"
        "-framework AudioToolbox")
elseif(WIN32)
    target_link_libraries(_core PRIVATE ole32)  # COM, used by the WASAPI backend
elseif(UNIX)
    find_package(Threads REQUIRED)
    target_link_libraries(_core PRIVATE Threads::Threads ${CMAKE_DL_LIBS} m)
endif()

# --- Optional native OSC: vendored tinyosc, gated on SOFTCUT_ENABLE_TINYOSC ---
# Everything tinyosc-related is confined to this block so the default build is
# untouched. Defines SOFTCUT_TINYOSC for _core.cpp to guard its OSC symbols.
if(SOFTCUT_ENABLE_TINYOSC)
    message(STATUS "softcut: native OSC enabled (vendored tinyosc)")
    target_sources(_core PRIVATE ${TINYOSC}/tinyosc.c)
    target_include_directories(_core PRIVATE ${TINYOSC})
    target_compile_definitions(_core PRIVATE SOFTCUT_TINYOSC)

    # tinyosc is vendored third-party C; keep its warnings out of our
    # -Wall -Wextra signal (intentional network-order casts and sign compares).
    if(MSVC)
        set_source_files_properties(${TINYOSC}/tinyosc.c PROPERTIES COMPILE_OPTIONS "/w")
    else()
        set_source_files_properties(${TINYOSC}/tinyosc.c PROPERTIES COMPILE_OPTIONS "-w")
    endif()

    # winsock provides htonl/htonll used by tinyosc's wire codec on Windows.
    if(WIN32)
        target_link_libraries(_core PRIVATE ws2_32)
    endif()
endif()

install(TARGETS _core DESTINATION softcut)
