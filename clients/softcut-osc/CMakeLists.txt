# Standalone softcut OSC server (no Python). Built as its own CMake project,
# separate from the scikit-build wheel:
#   cmake -S clients/softcut-osc -B build/softcut-osc -DCMAKE_BUILD_TYPE=Release
#   cmake --build build/softcut-osc
# (or `make build-standalone` from the repo root).
cmake_minimum_required(VERSION 3.15)
project(softcut_osc LANGUAGES CXX C)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

# Repo root is two levels up (clients/softcut-osc/..).
get_filename_component(ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)
set(SOFTCUT_LIB ${ROOT}/thirdparty/softcut-lib/softcut-lib)
set(MINIAUDIO ${ROOT}/thirdparty/miniaudio)
set(TINYOSC ${ROOT}/thirdparty/tinyosc)
set(DR_WAV ${ROOT}/thirdparty/dr_wav)

add_executable(softcut-osc
    main.cpp
    dr_wav_impl.c
    ${MINIAUDIO}/miniaudio.c
    ${TINYOSC}/tinyosc.c
    ${SOFTCUT_LIB}/src/Voice.cpp
    ${SOFTCUT_LIB}/src/ReadWriteHead.cpp
    ${SOFTCUT_LIB}/src/SubHead.cpp
    ${SOFTCUT_LIB}/src/FadeCurves.cpp
    ${SOFTCUT_LIB}/src/Svf.cpp)

target_include_directories(softcut-osc PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${ROOT}/src                # for shared/*.hpp (Python-free core)
    ${SOFTCUT_LIB}/include
    ${SOFTCUT_LIB}/src
    ${MINIAUDIO}
    ${TINYOSC}
    ${DR_WAV})

# Match the extension build: realtime device I/O only, no miniaudio codecs.
target_compile_definitions(softcut-osc PRIVATE MA_NO_DECODING MA_NO_ENCODING)

if(MSVC)
    target_compile_options(softcut-osc PRIVATE /W4)
    target_compile_definitions(softcut-osc PRIVATE _USE_MATH_DEFINES)
else()
    target_compile_options(softcut-osc PRIVATE -Wall -Wextra)
endif()

# Vendored C: keep third-party warnings (tinyosc casts, dr_wav) out of our signal.
if(MSVC)
    set_source_files_properties(${TINYOSC}/tinyosc.c dr_wav_impl.c PROPERTIES COMPILE_OPTIONS "/w")
else()
    set_source_files_properties(${TINYOSC}/tinyosc.c dr_wav_impl.c PROPERTIES COMPILE_OPTIONS "-w")
endif()

find_package(Threads REQUIRED)
target_link_libraries(softcut-osc PRIVATE Threads::Threads)
if(APPLE)
    target_link_libraries(softcut-osc PRIVATE
        "-framework CoreFoundation" "-framework CoreAudio" "-framework AudioToolbox")
elseif(WIN32)
    target_link_libraries(softcut-osc PRIVATE ws2_32 ole32)
elseif(UNIX)
    target_link_libraries(softcut-osc PRIVATE ${CMAKE_DL_LIBS} m)
endif()
