#!/usr/bin/env bash
set -euo pipefail
root=$(pwd -P)
old=/home/cassius/Videos/twins-isolated-20261118/out
out=$root/tcp-results
mkdir -p "$out"
trap 'rc=$?; printf "%s\n" "$rc" > "$out/exit-status"' EXIT
test -s "$old/export/all-finished.json"
exec 9>"$old/exclusive-campaign.lock"
flock 9
while [[ -n $(nvidia-smi --query-compute-apps=pid --format=csv,noheader) ]]; do sleep 10; done
nvidia-smi -q > "$out/gpu.txt"
lscpu > "$out/cpu.txt"
cp revision "$out/"
cmake -S . -B tcp-build -G Ninja -DTWINS_GPU_BACKEND=CUDA -DCMAKE_CUDA_ARCHITECTURES=120 \
    -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc > "$out/build.log" 2>&1
cmake --build tcp-build --target twins -j6 >> "$out/build.log" 2>&1
python3 experiments/paper_tcp_options/native.py build /home/cassius/Videos/netsim/unison . tcp-native
cp tcp-native/build.{json,log} "$out/"
sha256sum tcp-build/twins* tcp-native/reference >"$out/binaries.sha256"
python3 experiments/paper_full_review/tcp_timing.py tcp-build tcp-native/reference tcp-inputs "$out"
