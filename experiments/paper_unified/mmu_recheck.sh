#!/usr/bin/env bash
set -euo pipefail
bin=$(realpath "$1")
input=$(realpath "$2")
out=$(realpath -m "$3")
mkdir "$out"
for case in safe-small pause-resume unsigned-wrap low-headroom multislot nvl-idle-global-window; do
    b=1
    cc=(CC_MODE=3)
    case "$case" in
        safe-small) b=16;;
        unsigned-wrap) b=0;;
        pause-resume) cc=(HAS_WIN=0 CC_MODE=1 ENABLE_QCN=0);;
    esac
    for mode in safe serial window window-serial; do
        extra=()
        if [[ $mode == window* ]]; then extra+=(--gwin=4);fi
        if [[ $mode == *serial ]]; then extra+=(--serial);fi
        work="$out/$case/$mode"
        mkdir -p "$work"
        (
            cd "$work"
            rc=0
            timeout 70 "$bin" "$input/config.cfg" "--topo=$input/$case/topo" "--flows=$input/$case/flows.csv" \
                "BUFFER_SIZE=$b" "${cc[@]}" SIMULATOR_STOP_TIME=0.005 "${extra[@]}" --out=fct --txtrace=tx \
                --txtrace-cap=1000000 --fp-bases=1000003,1000033 >process.log 2>&1 || rc=$?
            printf "%s\n" "$rc" >rc
        )
    done
done
