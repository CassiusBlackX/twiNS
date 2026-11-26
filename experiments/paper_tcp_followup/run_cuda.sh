#!/usr/bin/env bash
set -euo pipefail
root=$(pwd -P)
out=$root/results
old=/home/cassius/Videos/twins-isolated-20261118/out
mkdir -p "$out"
trap 'rc=$?; printf "%s\n" "$rc" > "$out/exit-status"' EXIT
test -s "$old/export/all-finished.json"
exec 9>"$old/exclusive-campaign.lock"
flock 9
while :; do
    clients=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader)
    [[ -n $clients ]] || break
    sleep 15
done
nvidia-smi -q > "$out/gpu.txt"
cp revision "$out/"
sha256sum --quiet -c inputs.sha256
cmake -S . -B build -G Ninja -DTWINS_GPU_BACKEND=CUDA -DCMAKE_CUDA_ARCHITECTURES=120 \
    -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc > "$out/build.log" 2>&1
cmake --build build --target twins -j6 >> "$out/build.log" 2>&1
/usr/local/cuda/bin/nvcc -std=c++17 -O3 -lineinfo --fmad=false --expt-relaxed-constexpr -arch=sm_120 \
    -DTWINS_TCP_OPTIONS -DTWINS_LINK_TIMING -DTWINS_TCP_TIMER_ORDER -DTWINS_FRAGMENT_PROBE -DTWINS_TRACE_FRAGMENTS \
    -Isrc experiments/paper_tcp_followup/fragment-gpu.cu -o build/fragment-probe >> "$out/build.log" 2>&1
sha256sum build/twins* build/fragment-probe > "$out/binaries.sha256"
status=0
python3 experiments/paper_tcp_followup/accept.py build inputs expected.json "$out/targeted" || status=1
python3 experiments/paper_tcp_options/campaign.py matrix build/twins inputs "$out/matrix" references.json || status=1
python3 experiments/paper_tcp_options/regression.py run build/twins . release-inventory.json "$out/regression" || status=1
exit "$status"
