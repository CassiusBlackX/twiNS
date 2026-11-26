#!/usr/bin/env bash
set -euo pipefail
root=$(pwd -P)
old=/home/cassius/Videos/twins-isolated-20261118/out
out=$root/results
mkdir "$out"
trap 'rc=$?; printf "%s\n" "$rc" >"$out/exit-status"' EXIT
test -s "$old/export/all-finished.json"
exec 9>"$old/exclusive-campaign.lock"
flock 9
while [[ -n $(nvidia-smi --query-compute-apps=pid --format=csv,noheader) ]]; do sleep 10; done
nvcc=/usr/local/cuda/bin/nvcc
nvidia-smi -q > "$out/gpu.txt"
lscpu > "$out/cpu.txt"
cp -al "$root/../tcp-build" "$root/binaries"
rm "$root/binaries/twins2048-options-links" "$root/binaries/tcp_retry.py"
cp src/tcp_retry.py binaries/
build() {
    local target=$1
    shift
    "$nvcc" -std=c++17 -O3 -lineinfo --fmad=false --expt-relaxed-constexpr -arch=sm_120 \
        "$@" -Isrc src/main.cu -o "$target"
}
flags=(-DTWINS_LINK_TIMING -DTWINS_TCP_OPTIONS -DTWINS_TCP_TIMER_ORDER -DTWINS_CHAIN_RUNS=127)
build binaries/twins2048-options-links "${flags[@]}" >"$out/build.log" 2>&1
sha256sum binaries/twins2048-options-links ../tcp-native/reference >"$out/binaries.sha256"
python3 experiments/paper_full_review/tcp_timing.py binaries ../tcp-native/reference ../tcp-inputs "$out"
build binaries/twins2048-audit "${flags[@]}" -DTWINS_SCALE_HIST -DTWINS_TCP_BUCKET_AUDIT >>"$out/build.log" 2>&1
for scale in 1024 128; do
  for sack in 0 1; do
      dir="$out/layout/$scale-$sack"
      mkdir -p "$dir"
      timeout 90s binaries/twins2048-audit --single-pass --tcp=dctcp --tcp-timestamps=1 --tcp-sack="$sack" \
        --topo=graph "--topofile=../tcp-inputs/$scale.graph" --graph_k=16 "--flows=../tcp-inputs/$scale.csv" \
        "--out=$dir/flows.csv" >"$dir/run.log" 2>&1
  done
done
