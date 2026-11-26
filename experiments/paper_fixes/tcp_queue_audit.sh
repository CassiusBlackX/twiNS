#!/usr/bin/env bash
set -euo pipefail
binary=$(realpath "$1")
input=$(realpath "$2")
out=$(realpath -m "$3")
mkdir "$out"
sha256sum "$binary" "$input/128.csv" "$input/128.graph" >"$out/inputs.sha256"
for cc in dctcp cubic; do
    for sack in 0 1; do
        dir="$out/$cc-$sack-split"
        mkdir "$dir"
        (
            cd "$dir"
            rc=0
            TWINS_TCP_TRACE_TIMERS=1 timeout 100 "$binary" --single-pass "--tcp=$cc" --queue=200 --spray=0 \
                "--tcp-sack=$sack" --tcp-timestamps=1 --topo=graph "--topofile=$input/128.graph" --graph_k=16 \
                "--flows=$input/128.csv" --out=flows.csv --qtrace=queue.txt --qtrace_until=300000000000 >run.log 2>&1 || rc=$?
            printf '%s\n' "$rc" >rc
        )
    done
done
