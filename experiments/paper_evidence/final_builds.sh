#!/bin/bash
set -eu
cd "$(dirname "$0")/../.."
R=/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116/receipts
for variant in clean clean57 profile strict profile57; do
  dir=build-final-$variant
  defs=
  targets='twins twins128 twins-rdma twins-ai twins-astra-stub twins-island'
  case $variant in
    clean57) defs='TWINS_CHAIN_RUNS=57'; targets=twins ;;
    profile) defs='TWINS_EVIDENCE;RDMA_PROF' ;;
    strict) defs=TWINS_NO_FP; targets='twins-rdma twins-ai twins-astra-stub twins-island' ;;
    profile57) defs='TWINS_EVIDENCE;TWINS_CHAIN_RUNS=57'; targets=twins ;;
  esac
  cmake -S . -B "$dir" -DTWINS_GPU_BACKEND=HIP -DCMAKE_HIP_COMPILER=/opt/rocm/llvm/bin/clang++ -DCMAKE_PREFIX_PATH=/opt/rocm -DTWINS_EXTRA_DEFS="$defs" >"$R/final-$variant-configure.log" 2>&1
  cmake --build "$dir" --target $targets -j2 >"$R/final-$variant-build.log" 2>&1
  cp "$dir/CMakeCache.txt" "$R/final-$variant-CMakeCache.txt"
  echo 0 >"$R/final-$variant-build.rc"
done
