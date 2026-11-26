#!/usr/bin/env bash
set -euo pipefail

root=$(pwd -P)
old=/home/cassius/Videos/twins-isolated-20261118/out
out=$root/results
tools=$root/experiments/paper_tcp_options
mkdir -p "$out"
trap 'rc=$?; printf "%s\n" "$rc" > "$out/exit-status"' EXIT
test -s "$old/export/all-finished.json"
exec 9>"$old/exclusive-campaign.lock"
flock 9
while :; do
    clients=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader)
    [[ -n $clients ]] || break
    sleep 30
done
lscpu > "$out/cpu.txt"
nvidia-smi -q > "$out/gpu.txt"
df -h "$root" > "$out/disk.txt"
(cd inputs && sha256sum -c inputs.sha256) > "$out/input-check.txt"
sha256sum --quiet -c release-inputs.sha256
cp revision "$out/"
cmake -S . -B build -G Ninja -DTWINS_GPU_BACKEND=CUDA -DCMAKE_CUDA_ARCHITECTURES=120 \
    -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc > "$out/build.log" 2>&1
cmake --build build --target twins -j6 >> "$out/build.log" 2>&1
sha256sum build/twins* > "$out/binaries.sha256"
status=0
if python3 "$tools/campaign.py" matrix build/twins inputs "$out/matrix" references.json; then
    python3 "$tools/native.py" build /home/cassius/Videos/netsim/unison . "$out/native-build"
    python3 "$tools/campaign.py" timing build/twins inputs "$out/timing" "$out/native-build/reference" || status=1
else
    status=1
fi
python3 "$tools/regression.py" run build/twins . release-inventory.json "$out/regression" || status=1
exit "$status"
