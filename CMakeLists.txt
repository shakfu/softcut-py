cmake_minimum_required(VERSION 3.15...3.31)
project(${SKBUILD_PROJECT_NAME} VERSION ${SKBUILD_PROJECT_VERSION} LANGUAGES CXX C)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

find_package(Python REQUIRED COMPONENTS Interpreter Development.Module)
find_package(nanobind CONFIG REQUIRED)

# softcut-lib: the per-voice DSP engine we are wrapping. It owns no buffer
# memory and has no external dependencies, so we compile its sources straight
# into the extension module.
set(SOFTCUT_LIB ${CMAKE_CURRENT_SOURCE_DIR}/thirdparty/softcut-lib/softcut-lib)

# miniaudio: single-header realtime audio device backend (CoreAudio/ALSA/WASAPI).
# miniaudio.c is the one translation unit that defines MINIAUDIO_IMPLEMENTATION.
set(MINIAUDIO ${CMAKE_CURRENT_SOURCE_DIR}/thirdparty/miniaudio)

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
else()
    target_compile_options(_core PRIVATE -Wall -Wextra)
endif()

# Platform audio backends required by miniaudio.
if(APPLE)
    target_link_libraries(_core PRIVATE
        "-framework CoreFoundation"
        "-framework CoreAudio"
        "-framework AudioToolbox")
    find_library(PTHREAD_LIB pthread)
elseif(UNIX)
    find_package(Threads REQUIRED)
    target_link_libraries(_core PRIVATE Threads::Threads ${CMAKE_DL_LIBS} m)
endif()

install(TARGETS _core DESTINATION softcut)
