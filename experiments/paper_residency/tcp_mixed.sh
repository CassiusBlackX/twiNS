#!/usr/bin/env bash
set -euo pipefail
wide=$(realpath "$1")
split=$(realpath "$2")
input=$(realpath "$3")
out=$(realpath -m "$4")
mkdir "$out"
sha256sum "$wide" "$split" "$input/128.csv" "$input/128.graph" >"$out/inputs.sha256"
for cc in dctcp cubic; do
    for sack in 0 1; do
        for layout in wide split; do
            (
                mkdir "$out/$cc-$sack-$layout"
                cd "$out/$cc-$sack-$layout"
                bin=$wide
                [[ $layout == wide ]] || bin=$split
                rc=0
                TWINS_TCP_TRACE_TIMERS=1 timeout 140s "$bin" --single-pass "--tcp=$cc" --queue=200 --spray=0 \
                    "--tcp-sack=$sack" --tcp-timestamps=1 --topo=graph "--topofile=$input/128.graph" --graph_k=16 \
                    "--flows=$input/128.csv" --out=flows.csv --qtrace=queue.txt --qtrace_until=300000000000 >run.log 2>&1 || rc=$?
                printf '%s\n' "$rc" >rc
            )
        done
    done
done
