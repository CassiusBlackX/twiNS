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

# twins: 64-byte frames, event histories of 9 runs; twins128: 128-byte frames, 25 runs, for the
# rare runs in which twins reports undecided ties (equally timed histories longer than 9 runs)
foreach(T twins twins128)
    add_executable(${T} src/main.cu)
    target_include_directories(${T} PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)
    if(T STREQUAL "twins128")
        target_compile_definitions(${T} PRIVATE TWINS_CHAIN_RUNS=25)
    endif()
    # Exactness needs ns-3's floating-point results: no FMA contraction except where src/nsmath.h
    # and the model write fma() explicitly.
    if(TWINS_GPU_BACKEND STREQUAL "HIP")
        find_package(hip REQUIRED)
        set_source_files_properties(src/main.cu PROPERTIES LANGUAGE HIP)
        set(CMAKE_HIP_STANDARD 17)
        set(CMAKE_HIP_FLAGS_RELEASE "-O3")
        target_compile_definitions(${T} PRIVATE TWINS_USE_HIP)
        target_compile_options(${T} PRIVATE -ffp-contract=off -Wno-unused-value)
        # promote-alloca moves private arrays into LDS, which caps occupancy
        target_compile_options(${T} PRIVATE "SHELL:-mllvm -disable-promote-alloca-to-lds")
        target_link_libraries(${T} PRIVATE hip::host hip::device)
    else()
        set(CMAKE_CUDA_STANDARD 17)
        set(CMAKE_CUDA_FLAGS_RELEASE "-O3 -lineinfo --fmad=false")
        set(CMAKE_CUDA_FLAGS "${CMAKE_CUDA_FLAGS} --expt-relaxed-constexpr")
        set_source_files_properties(src/main.cu PROPERTIES LANGUAGE CUDA)
        find_package(CUDAToolkit REQUIRED)
        target_link_libraries(${T} PRIVATE CUDA::cudart)
    endif()
endforeach()

# twins-rdma: the RDMA model (src/rdma), validated against the HPCC simulator (validation/hpcc)
add_executable(twins-rdma src/rdma/rmain.cu)
target_include_directories(twins-rdma PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)
if(TWINS_GPU_BACKEND STREQUAL "HIP")
    set_source_files_properties(src/rdma/rmain.cu PROPERTIES LANGUAGE HIP)
    target_compile_definitions(twins-rdma PRIVATE TWINS_USE_HIP)
    target_compile_options(twins-rdma PRIVATE -ffp-contract=off -Wno-unused-value)
    target_link_libraries(twins-rdma PRIVATE hip::host hip::device)
else()
    set_source_files_properties(src/rdma/rmain.cu PROPERTIES LANGUAGE CUDA)
    target_link_libraries(twins-rdma PRIVATE CUDA::cudart)
endif()

# astra-sim's system layer over TwiNS (docs/ai/whatif.md): SimAI's code is fetched by
# src/astra/fetch.sh into out/astra; without it these targets do not exist
set(ASTRA_ROOT ${CMAKE_CURRENT_SOURCE_DIR}/out/astra/SimAI/astra-sim-alibabacloud)
if(EXISTS ${ASTRA_ROOT}/astra-sim/system/Sys.cc)
    enable_language(CXX)
    file(GLOB ASTRA_SRC CONFIGURE_DEPENDS
        ${ASTRA_ROOT}/astra-sim/system/*.cc
        ${ASTRA_ROOT}/astra-sim/system/collective/*.cc
        ${ASTRA_ROOT}/astra-sim/system/fast-backend/*.cc
        ${ASTRA_ROOT}/astra-sim/system/memory/*.cc
        ${ASTRA_ROOT}/astra-sim/system/scheduling/*.cc
        ${ASTRA_ROOT}/astra-sim/system/topology/*.cc
        ${ASTRA_ROOT}/astra-sim/workload/*.cc
        ${CMAKE_CURRENT_SOURCE_DIR}/out/astra/inc/SimCCL/mock/*.cc)
    list(FILTER ASTRA_SRC EXCLUDE REGEX ".*(BootStrapnet|SimAiFlowModelRdma|PhyMultiThread)\\.cc")
    add_library(astra-frontend STATIC ${ASTRA_SRC} src/astra/frontend.cc)
    target_include_directories(astra-frontend PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src ${ASTRA_ROOT} ${CMAKE_CURRENT_SOURCE_DIR}/out/astra/inc)
    target_compile_options(astra-frontend PRIVATE -O3 -w)
    # The system layer spends about a quarter of its time in malloc and free; tcmalloc saves about 20%,
    # and some memory. Optional.
    find_library(TWINS_TCMALLOC NAMES tcmalloc_minimal libtcmalloc_minimal.so.4)
    set_target_properties(astra-frontend PROPERTIES CXX_STANDARD 17)
    add_executable(twins-astra-stub src/astra/stub.cc)
    target_link_libraries(twins-astra-stub PRIVATE astra-frontend pthread)
    if(TWINS_TCMALLOC)
        target_link_libraries(twins-astra-stub PRIVATE ${TWINS_TCMALLOC})
    endif()
    set_target_properties(twins-astra-stub PROPERTIES CXX_STANDARD 17)
    # twins-island: one server's NVLink part on a CPU core, for twins-ai --nvl-islands (src/rdma/island.h)
    add_executable(twins-island src/rdma/island_main.cc)
    target_include_directories(twins-island PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)
    target_compile_definitions(twins-island PRIVATE TWINS_CPU TWINS_ASTRA TWINS_ISLAND)
    target_compile_options(twins-island PRIVATE -O3 -march=native -ffp-contract=off -Wno-unused-value -Wno-unknown-attributes)
    target_link_libraries(twins-island PRIVATE rt)
    set_target_properties(twins-island PROPERTIES CXX_STANDARD 17)
    # twins-ai: twins-rdma with the system layer on the host, no recording needed
    add_executable(twins-ai src/rdma/rmain.cu)
    target_include_directories(twins-ai PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)
    target_compile_definitions(twins-ai PRIVATE TWINS_ASTRA)
    if(TWINS_GPU_BACKEND STREQUAL "HIP")
        target_compile_definitions(twins-ai PRIVATE TWINS_USE_HIP)
        target_compile_options(twins-ai PRIVATE -ffp-contract=off -Wno-unused-value)
        target_link_libraries(twins-ai PRIVATE hip::host hip::device astra-frontend pthread)
        if(TWINS_TCMALLOC)
            target_link_libraries(twins-ai PRIVATE ${TWINS_TCMALLOC})
        endif()
    else()
        target_link_libraries(twins-ai PRIVATE CUDA::cudart astra-frontend pthread)
    endif()
endif()
