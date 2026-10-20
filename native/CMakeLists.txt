cmake_minimum_required(VERSION 3.16)
project(encodec_live_native LANGUAGES CXX)

option(ENCODEC_ENABLE_OPENMP "Enable Eigen/OpenMP encoder threading" ON)
set(
    ENCODEC_CPP_SOURCE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/encodec"
    CACHE PATH "Path to an encodec.cpp checkout or the bundled offline snapshot"
)

if(EXISTS "${ENCODEC_CPP_SOURCE_DIR}/src/lib/encodec.cpp")
    set(ENCODEC_CPP_SOURCE "${ENCODEC_CPP_SOURCE_DIR}/src/lib/encodec.cpp")
    set(ENCODEC_CPP_INCLUDE_DIR "${ENCODEC_CPP_SOURCE_DIR}/src/lib")
elseif(EXISTS "${ENCODEC_CPP_SOURCE_DIR}/encodec.cpp")
    set(ENCODEC_CPP_SOURCE "${ENCODEC_CPP_SOURCE_DIR}/encodec.cpp")
    set(ENCODEC_CPP_INCLUDE_DIR "${ENCODEC_CPP_SOURCE_DIR}")
else()
    message(FATAL_ERROR "encodec.cpp source not found in ${ENCODEC_CPP_SOURCE_DIR}")
endif()

add_executable(
    encodec-live-native
    worker.cpp
    "${ENCODEC_CPP_SOURCE}"
)
target_compile_features(encodec-live-native PRIVATE cxx_std_20)
target_compile_definitions(encodec-live-native PRIVATE ENCODEC_RUNTIME_MODELS_ONLY=1)
target_include_directories(
    encodec-live-native
    PRIVATE
    "${ENCODEC_CPP_INCLUDE_DIR}"
    third_party/eigen
)
target_compile_options(
    encodec-live-native
    PRIVATE
    -O3 -ffunction-sections -fdata-sections
)
if(APPLE)
    target_link_options(encodec-live-native PRIVATE -Wl,-dead_strip)
else()
    target_link_options(encodec-live-native PRIVATE -Wl,--gc-sections)
endif()

if(ENCODEC_ENABLE_OPENMP)
    find_package(OpenMP QUIET)
    if(OpenMP_CXX_FOUND)
        target_compile_definitions(encodec-live-native PRIVATE ENCODEC_HAS_OPENMP=1)
        target_link_libraries(encodec-live-native PRIVATE OpenMP::OpenMP_CXX)
    else()
        message(STATUS "OpenMP not found; building the native encoder single-threaded")
        target_compile_definitions(encodec-live-native PRIVATE EIGEN_DONT_PARALLELIZE=1)
    endif()
else()
    target_compile_definitions(encodec-live-native PRIVATE EIGEN_DONT_PARALLELIZE=1)
endif()

install(TARGETS encodec-live-native RUNTIME DESTINATION bin)
