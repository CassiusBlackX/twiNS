cmake_minimum_required(VERSION 3.21)

set(TWINS_GPU_BACKEND "HIP" CACHE STRING "GPU backend: HIP or CUDA")
set_property(CACHE TWINS_GPU_BACKEND PROPERTY STRINGS HIP CUDA)

if(TWINS_GPU_BACKEND STREQUAL "HIP")
    if(NOT DEFINED CMAKE_HIP_ARCHITECTURES)
        set(CMAKE_HIP_ARCHITECTURES gfx1200)
    endif()
    project(TwiNS LANGUAGES CXX HIP)
else()
    project(TwiNS LANGUAGES CXX CUDA)
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

add_executable(twins src/main.cu)
target_include_directories(twins PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)

# Exactness needs ns-3's floating-point results: no FMA contraction except where src/nsmath.h
# and the model write fma() explicitly.
if(TWINS_GPU_BACKEND STREQUAL "HIP")
    find_package(hip REQUIRED)
    set_source_files_properties(src/main.cu PROPERTIES LANGUAGE HIP)
    set(CMAKE_HIP_STANDARD 17)
    set(CMAKE_HIP_FLAGS_RELEASE "-O3")
    target_compile_definitions(twins PRIVATE TWINS_USE_HIP)
    target_compile_options(twins PRIVATE -ffp-contract=off -Wno-unused-value)
    # promote-alloca moves private arrays into LDS, which caps occupancy
    target_compile_options(twins PRIVATE "SHELL:-mllvm -disable-promote-alloca-to-lds")
    target_link_libraries(twins PRIVATE hip::host hip::device)
else()
    set(CMAKE_CUDA_STANDARD 17)
    set(CMAKE_CUDA_FLAGS_RELEASE "-O3 -lineinfo --fmad=false")
    set(CMAKE_CUDA_FLAGS "${CMAKE_CUDA_FLAGS} --expt-relaxed-constexpr")
    set_source_files_properties(src/main.cu PROPERTIES LANGUAGE CUDA)
    find_package(CUDAToolkit REQUIRED)
    target_link_libraries(twins PRIVATE CUDA::cudart)
endif()
