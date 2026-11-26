#!/usr/bin/env bash
set -uo pipefail
p=$(pwd -P)
old=/home/cassius/Videos/twins-isolated-20261118/out
test -s "$old/export/all-finished.json" || exit 1
exec 9>"$old/exclusive-campaign.lock"
flock 9
while [[ -n $(nvidia-smi --query-compute-apps=pid --format=csv,noheader) ]]; do sleep 5; done
original=/home/cassius/Videos/twins-full-review-20261126
bash cubic_gpu.sh "$original/tcp-v2/binaries/twins" k8_a2a30.csv cubic-cuda
for ((i=0;i<120;i++)); do [[ -f twins-paper-tcp-options/build.rc ]] && break; sleep 5; done
test "$(cat twins-paper-tcp-options/build.rc)" = 0 || exit 1
python3 closed_cases.py twins-paper-tcp-options/build/twins-ai . cuda-closed
rc=$?
input=$original/rdma-candidate/inputs/k40
mkdir -p strong
date +%s%N > strong/start
timeout 180s twins-paper-tcp-options/build/twins-rdma "$input/strong.cfg" "--topo=$input/topo.log" "--flows=$input/incast.csv" \
 --q3cap=2048 --q3-auto --fp-bases=1000003,1000033 CC_MODE=1 --out=strong/output.fct > strong/run.log 2>&1
printf '%s\n' "$?" > strong/rc
date +%s%N > strong/end
exit "$rc"
