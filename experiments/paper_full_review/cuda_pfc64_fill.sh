#!/usr/bin/env bash
set -euo pipefail
root=$(pwd -P)
out=$root/results
other=$root/../cuda-diagnostics
old=/home/cassius/Videos/twins-isolated-20261118/out
mkdir "$out"
trap 'rc=$?; printf "%s\n" "$rc" >"$out/exit-status"' EXIT
while [[ ! -f "$other/results/exit-status" ]]; do sleep 5; done
if [[ -f "$other/results/pfc/64/serial/rc" ]] && [[ $(cat "$other/results/pfc/64/serial/rc") == 0 ]]; then
    printf '64 MiB already collected\n' >"$out/status"
    exit
fi
exec 9>"$old/exclusive-campaign.lock"
flock 9
while [[ -n $(nvidia-smi --query-compute-apps=pid --format=csv,noheader) ]]; do sleep 10; done
source=/home/cassius/Videos/twins-paper-strict-20261123/pfc_gpu_boundary/20261123T142123Z/results/pfc/pool-64/input
for mode in checked serial; do
    target="$out/64/$mode"
    mkdir -p "$target"
    more=()
    if [[ "$mode" == serial ]]; then more+=(--serial); fi
    (cd "$target" && /usr/bin/time -v timeout 60s taskset -c 0 "$other/build/twins-ai" \
      "$source/config.log" "--topo=$source/topo.log" "--astra=$source/workload.log" --out=messages.fct --dag-out=actions.dag \
      --nvl-islands=0 --gwin=0 --fp-bases=1000003,1000033 "${more[@]}" >run.log 2>&1 && printf '0\n' >rc)
done
