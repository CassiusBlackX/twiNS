#!/usr/bin/env bash
set -euo pipefail
r=$(pwd -P)
base=/home/cassius/Videos/twins-full-review-20261126
old=/home/cassius/Videos/twins-isolated-20261118/out
bin=$r/memory-binaries
mkdir "$bin"
cp "$base/tcp-v2/binaries/"{twins,twins512-options} "$bin/"
cp src/tcp_retry.py "$bin/"
build() {
  /usr/local/cuda/bin/nvcc -O3 --fmad=false -lineinfo --expt-relaxed-constexpr -std=c++17 -arch=sm_120 -Isrc \
    -DTWINS_TCP_OPTIONS -DTWINS_LINK_TIMING -DTWINS_TCP_TIMER_ORDER "-DTWINS_CHAIN_RUNS=$1" src/main.cu -o "$bin/$2"
}
build 31 twins512-options-links > memory-build.log 2>&1
build 127 twins2048-options-links >> memory-build.log 2>&1
test -s "$old/export/all-finished.json"
exec 9>"$old/exclusive-campaign.lock"
flock 9
while [[ -n $(nvidia-smi --query-compute-apps=pid --format=csv,noheader) ]]; do sleep 5; done
python3 experiments/paper_boundary/pid_memory.py "$bin" "$base/tcp-inputs" "$r/../phase-memory" \
  --phase-logs --expected "$(cat ../memory.expected)"
