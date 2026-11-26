#!/usr/bin/env bash
set -euo pipefail
binary=$(realpath "$1")
inputs=$(realpath "$2")
out=$(realpath -m "$3")
[[ -x $binary ]] || { echo "missing executable: $binary" >&2; exit 1; }
mkdir "$out"
for fixture in r200d8 r400d9; do
    for mode in regular one-slot island; do
        work="$out/$fixture/$mode"
        mkdir -p "$work"
        (
            cd "$work"
            export TWINS_CLOSED_CALLBACK_TRACE=callbacks.txt
            export TWINS_CLOSED_HORIZON_TRACE=horizons.txt
            [[ $mode != one-slot ]] || export TWINS_CL_ONE_SLOT=1
            islands=0
            [[ $mode != island ]] || islands=1
            rc=0
            timeout 150 "$binary" "$inputs/$fixture/config.log" "--topo=$inputs/$fixture/topo.log" \
                "--astra=$inputs/$fixture/simai-workload" --out=messages.fct --dag-out=actions.dag \
                "--nvl-islands=$islands" --gwin=0 --fp-bases=1000003,1000033 >run.log 2>&1 || rc=$?
            printf '%s\n' "$rc" >rc
        )
    done
done
