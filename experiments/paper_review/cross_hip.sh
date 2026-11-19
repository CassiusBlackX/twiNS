#!/usr/bin/env bash
set -euo pipefail
root=$(pwd -P)
prior=/home/cassius/netsim/twins-paper-scale-20261119
toolchain=$prior/toolchain
compiler_bin="${TWINS_HIP_COMPILER_BIN:-$toolchain/llvm/bin}"
resource_dir="${TWINS_HIP_RESOURCE_DIR:-$toolchain/llvm/lib/clang/22}"
export PATH="$compiler_bin:$toolchain/llvm/bin:$PATH"
mkdir -p builds
target=$1
shift
source=src/rdma/rmain.cu
flags=(-O3)
libraries=()
case $target in
  irn-*) ;;
  ai-*) flags+=(-DTWINS_ASTRA); libraries+=("$prior/project/front/build/libastra-frontend.a" -pthread);;
  wide-*) source=src/main.cu
    flags=(-DTWINS_LINK_TIMING -DTWINS_CHAIN_RUNS=31 -DTWINS_TCP_TIMER_ORDER
           -fno-inline-functions)
    if [[ "${TWINS_TCP_LDS_PROMOTION:-off}" == off ]]; then
        flags+=(-mllvm -disable-promote-alloca-to-lds)
    fi;;
  *) exit 2;;
esac
set -x
clang++ -no-canonical-prefixes -B"$compiler_bin" \
  -resource-dir "$resource_dir" --rocm-path="$toolchain" \
  --hip-device-lib-path="$toolchain/amdgcn/bitcode" -I"$toolchain/include" -I"$root/src" \
  -DTWINS_USE_HIP -D__HIP_PLATFORM_AMD__=1 -std=gnu++17 --offload-arch=gfx1200 \
  -ffp-contract=off -Wno-unused-value "${flags[@]}" "$@" -x hip -c "$source" -o "builds/$target.o"
g++ "builds/$target.o" "${libraries[@]}" -L"$toolchain/lib" \
  -Wl,--allow-shlib-undefined -lamdhip64 -o "builds/$target.pending"
mv "builds/$target.pending" "builds/$target"
