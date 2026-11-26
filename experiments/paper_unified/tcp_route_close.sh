#!/usr/bin/env bash
set -euo pipefail
mode=$1
bin=$(realpath "$2")
input=$(realpath "$3")
out=$(realpath -m "$4")
mkdir "$out"
cd "$out"
rc=0
common=(--fin=1 --queue=20 --tcp-sack=1 --tcp-timestamps=1 "--topofile=$input/two-paths.graph" \
    "--flows=$input/route-close.csv" --fail=1007000:s0:s1,1013000:s0:s1:up --out=flows.csv)
if [[ $mode == native ]]; then
    TRACE_ALLQ=20000000000 TRACE_QMETA=1 timeout 45 "$bin" --topo=file --tcp=ns3::TcpNewReno --threads=0 \
        --flowEcmp=0 --hostq=red --stop=0.02 "${common[@]}" >run.log 2>&1 || rc=$?
    sed -n 's/^Q //p' run.log | LC_ALL=C sort -s -k1,1n -k2,2n -k3,3n >queue.txt
else
    timeout 55 "$bin" --topo=graph --tcp=newreno --spray=1 --drain --max_slots=26667 \
        --qtrace=queue.txt --qtrace-options --qtrace_until=19999999999 "${common[@]}" >run.log 2>&1 || rc=$?
fi
printf '%s\n' "$rc" >rc
