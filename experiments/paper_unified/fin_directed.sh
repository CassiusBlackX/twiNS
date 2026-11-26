#!/usr/bin/env bash
set -euo pipefail
mode=$1
bin=$(realpath "$2")
out=$(realpath -m "$3")
mkdir "$out"
for arm in data reply; do
    mkdir "$out/$arm"
    (
        cd "$out/$arm"
        cat >flows <<'FLOW'
src,dst,flow_size,start_ns
0,1,4678,0
2,3,20000417,800000
FLOW
        rc=0
        common=(--flows=flows --k=4 --fin=1 --tcp-sack=1 --tcp-timestamps=1 --queue=200 --out=flows.csv)
        if [[ $mode == native ]]; then
            TWINS_CLOSE_DROP="$arm" TRACE_ALLQ=10000000000 TRACE_QMETA=1 timeout 60 "$bin" --tcp=ns3::TcpNewReno \
                --threads=0 --flowEcmp=1 --hostq=red --stop=0.01 --traceWire=wire.raw --traceWireUntil=3000000000 \
                "${common[@]}" >run.log 2>&1 || rc=$?
            sed -n 's/^Q //p' run.log | LC_ALL=C sort -s -k1,1n -k2,2n -k3,3n >queue.txt
        else
            TWINS_CLOSE_DROP="$arm" TWINS_TCP_TRACE_TIMERS=1 timeout 60 "$bin" --single-pass --tcp=newreno \
                --drain --max_slots=10000 --qtrace=queue.txt --qtrace-options --qtrace_until=9999999999 \
                "${common[@]}" >run.log 2>&1 || rc=$?
        fi
        printf '%s\n' "$rc" >rc
    )
done
