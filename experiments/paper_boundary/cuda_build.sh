#!/usr/bin/env bash
set -euo pipefail
p=$(pwd -P)
log=$p/build.log
mkdir -p out
ln -s /home/cassius/Videos/twins-paper-followup-20261123/candidate/out/astra out/astra
cmake -S . -B build -G Ninja -DTWINS_GPU_BACKEND=CUDA -DCMAKE_CUDA_ARCHITECTURES=120 \
 -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc >"$log" 2>&1
cmake --build build --target twins-rdma twins-ai twins-island -j6 >>"$log" 2>&1
sha256sum build/twins-{rdma,ai,island} > binaries.sha256
