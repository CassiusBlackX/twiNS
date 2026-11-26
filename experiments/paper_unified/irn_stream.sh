#!/usr/bin/env bash
set -euo pipefail
bin=$(realpath "$1")
input=$(realpath "$2")
out=$(realpath -m "$3")
mkdir "$out"
for kind in plain ecn; do
    for mode in serial checked; do
        mkdir -p "$out/$kind/$mode"
        (
            cd "$out/$kind/$mode"
            flags=(--serial)
            [[ $mode == serial ]] || flags=(--irn-parallel)
            rc=0
            timeout 90 "$bin" "$input/103/$kind/128/spread/config" \
                "--topo=$input/103/$kind/128/uniform.graph" "--flows=$input/103/$kind/128/spread/flows.csv" \
                IRN_BDP=156000 --fp-bases=1000003,1000033 --out=flows.fct --txtrace=wire.raw --txtrace-cap=2000000 \
                "${flags[@]}" >run.log 2>&1 || rc=$?
            printf '%s\n' "$rc" >rc
        )
    done
done
