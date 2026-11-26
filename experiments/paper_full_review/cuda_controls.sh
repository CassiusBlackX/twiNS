#!/usr/bin/env bash
set -euo pipefail
root=$(pwd -P)
out=$root/results
old=/home/cassius/Videos/twins-isolated-20261118/out
diag=$root/../cuda-diagnostics
mkdir "$out"
trap 'rc=$?; printf "%s\n" "$rc" >"$out/exit-status"' EXIT
test -s "$old/export/all-finished.json"
while [[ ! -f "$diag/results/exit-status" ]]; do sleep 10; done
exec 9>"$old/exclusive-campaign.lock"
flock 9
while [[ -n $(nvidia-smi --query-compute-apps=pid --format=csv,noheader) ]]; do sleep 10; done
bin=$diag/build/twins-rdma
i=$root/../rdma-candidate/inputs
mkdir "$out/timer-minimal" "$out/rdma-batch" "$out/closed-tail"
timeout 80s "$bin" "$i/hpcc.cfg" "--topo=$i/fattree8.txt" "--flows=$root/minimal.csv" --delay=10000 \
    "--out=$out/timer-minimal/output.fct" CC_MODE=1 --fp-bases=1000003,1000033 >"$out/timer-minimal/run.log" 2>&1
d=$out/rdma-batch
printf 'src,dst,flow_size,start_ns\n0,1,1000,1000\n' >"$d/healthy.csv"
printf 'healthy %s %s CC_MODE=1\nloaded %s %s CC_MODE=1\n' "$i/fattree8.txt" "$d/healthy.csv" "$i/fattree8.txt" "$i/k8_a2a30.csv" >"$d/worlds"
timeout 120s "$bin" "$i/hpcc.cfg" "--batch=$d/worlds" "--outdir=$d" --fcap-auto=1 --fp-bases=1000003,1000033 >"$d/run.log" 2>&1
pfc=/home/cassius/Videos/twins-paper-strict-20261123/pfc_gpu_boundary/20261123T142123Z/results/pfc/pool-06/input
(
    cd "$out/closed-tail"
    rc=0
    timeout 60s taskset -c 0 "$root/../rdma-candidate/build/twins-ai" "$pfc/config.log" "--topo=$pfc/topo.log" "--astra=$pfc/workload.log" \
       --out=messages.fct --dag-out=actions.dag --nvl-islands=0 --gwin=0 --tail=1 --fp-bases=1000003,1000033 >run.log 2>&1 || rc=$?
    printf '%s\n' "$rc" >rc
)
PYTHONPATH="$root/../experiments/paper_tcp_options" python3 - "$root" "$out" <<'PY'
from pathlib import Path
import sys
from campaign import invoke
root,out=map(Path,sys.argv[1:])
ref=Path('/home/cassius/Videos/twins-paper-strict-20261123/rdma_scale_compact_reference/20261125T160521Z/results/frozen-k32')
binary=root/'../rdma-candidate/build/twins-rdma'
argv=['taskset','-c','0',binary, ref/'config.log',f'--topo={ref/"topo.log"}',f'--flows={ref/"distributed.csv"}',
      '--out=output.fct','CC_MODE=1','--q3cap=4096','--fp-bases=1000003,1000033']
row=invoke(list(map(str,argv)),out/'k32-q4096',gpu_sample=True)
raise SystemExit(row['rc'])
PY
