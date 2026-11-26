#!/usr/bin/env bash
set -euo pipefail
bin=$(realpath "$1")
input=$(realpath "$2")
out=$(realpath -m "$3")
mkdir "$out"
for mode in regular one-slot island; do
    if [[ $mode == island ]] && [[ ! -x $(dirname "$bin")/twins-island ]]; then continue; fi
    mkdir "$out/$mode"
    (
        cd "$out/$mode"
        export TWINS_CLOSED_CALLBACK_TRACE=callbacks.txt
        [[ $mode != one-slot ]] || export TWINS_CL_ONE_SLOT=1
        rc=0
        islands=0
        [[ $mode != island ]] || islands=1
        timeout 110 "$bin" "$input/config.log" "--topo=$input/topo.log" "--astra=$input/simai-workload" \
            --out=messages.fct --dag-out=actions.dag "--nvl-islands=$islands" --gwin=0 \
            --fp-bases=1000003,1000033 BUFFER_SIZE=64 >run.log 2>&1 || rc=$?
        printf '%s\n' "$rc" >rc
    )
done
