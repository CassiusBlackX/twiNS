#!/usr/bin/env bash
set -euo pipefail
mode=$1
bin=$(realpath "$2")
input=$(realpath "$3")
out=$(realpath -m "$4")
mkdir "$out"
for wire in 319 320 321; do
    payload=$((wire-94))
    mkdir "$out/$wire"
    (
        cd "$out/$wire"
        printf 'src,dst,flow_size,start_ns\n0,1,%s,0\n' "$payload" >flows.csv
        modes=()
        if [[ $mode != native ]]; then modes+=(--txtrace-cap=1024); fi
        for arm in default serial; do
            [[ $mode != native || $arm != serial ]] || continue
            rc=0
            serial=()
            [[ $arm != serial ]] || serial+=(--serial)
            TWINS_MMU_SNAPSHOT="$arm.boundaries" TWINS_MMU_STEP=680 timeout 25 "$bin" "$input/config.cfg" \
                "--topo=$input/low-headroom/topo" --flows=flows.csv CC_MODE=3 BUFFER_SIZE=1 SIMULATOR_STOP_TIME=0.005 \
                "--out=$arm.fct" "--txtrace=$arm.wire" "${modes[@]}" "${serial[@]}" >"$arm.log" 2>&1 || rc=$?
            printf '%s\n' "$rc" >"$arm.rc"
        done
    )
done
