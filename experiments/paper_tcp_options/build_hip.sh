#!/usr/bin/env bash
set -euo pipefail
root=$(pwd -P)
rocm=${TWINS_ROCM_CROSS:-/home/cassius/netsim/twins-paper-scale-20261119/toolchain}
compiler=$rocm/llvm/bin
output=$1
shift
mkdir -p "$(dirname "$output")"
export PATH="$compiler:$PATH"
set -x
clang++ -no-canonical-prefixes -B"$compiler" -resource-dir "$rocm/llvm/lib/clang/22" \
    --rocm-path="$rocm" --hip-device-lib-path="$rocm/amdgcn/bitcode" \
    -I"$rocm/include" -I"$root/src" -DTWINS_USE_HIP -D__HIP_PLATFORM_AMD__=1 \
    -std=gnu++17 --offload-arch=gfx1200 -ffp-contract=off -Wno-unused-value \
    -mllvm -disable-promote-alloca-to-lds -O3 "$@" -x hip -c src/main.cu -o "$output.o"
g++ "$output.o" -L"$rocm/lib" -Wl,--allow-shlib-undefined -lamdhip64 -o "$output.pending"
mv "$output.pending" "$output"
