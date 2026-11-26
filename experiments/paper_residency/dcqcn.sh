#!/usr/bin/env bash
set -euo pipefail
bin=$(realpath "$1")
input=$(realpath "$2")
out=$(realpath -m "$3")
mkdir "$out"
cd "$out"
rc=0
timeout 120 "$bin" "$input/hpcc.cfg" "--topo=$input/fattree8.txt" "--flows=$input/k8_a2a30.csv" \
    CC_MODE=1 --delay=2000ns --out=output.fct --txtrace=wire.raw --until=300000 \
    --fp-bases=1000003,1000033 >run.log 2>&1 || rc=$?
printf '%s\n' "$rc" >rc
