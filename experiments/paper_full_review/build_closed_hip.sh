#!/usr/bin/env bash
set -euo pipefail
output=$1
host_library=$2
rocm=${TWINS_ROCM_CROSS:-/home/cassius/netsim/twins-paper-scale-20261119/toolchain}
compiler=$rocm/llvm/bin
export PATH="$compiler:$PATH"
set -x
clang++ -no-canonical-prefixes -B"$compiler" -resource-dir "$rocm/llvm/lib/clang/22" \
    --rocm-path="$rocm" --hip-device-lib-path="$rocm/amdgcn/bitcode" \
    -I"$rocm/include" -Isrc -DTWINS_USE_HIP -D__HIP_PLATFORM_AMD__=1 -DTWINS_ASTRA -DRDMA_PFC_AUDIT \
    -std=gnu++17 --offload-arch=gfx1200 -ffp-contract=off -Wno-unused-value \
    -mllvm -disable-promote-alloca-to-lds -O2 -fno-inline-functions -x hip -c src/rdma/rmain.cu -o "$output.o"
g++ "$output.o" "$host_library" -pthread -L"$rocm/lib" -Wl,--allow-shlib-undefined -lamdhip64 -o "$output.tmp"
mv "$output.tmp" "$output"
