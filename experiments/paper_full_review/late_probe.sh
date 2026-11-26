#!/usr/bin/env bash
set -euo pipefail
binary=$(realpath "$1")
in=$(realpath "$2")
out=$(realpath -m "$3")
mkdir "$out"
export LD_LIBRARY_PATH="${4:-${LD_LIBRARY_PATH:-}}"
printf 'src,dst,flow_size,start_ns\n0,1,2000,1000\n2,3,4000,100000\n' >"$out/offered.csv"
run() {
    timeout 60s "$binary" "$in/hpcc.cfg" "--topo=$in/fattree8.txt" "--flows=$out/offered.csv" "--out=$out/$1.fct" \
      "--txtrace=$out/$1.wire" --until=120000 CC_MODE=1 >"$out/$1.log" 2>&1
}
run control
t=$(awk '$1==0 { print $2+$3 }' "$out/control.fct")
TWINS_DUPLICATE_NS=$((t-1)) run before
TWINS_DUPLICATE_NS=$t run equal-before
TWINS_DUPLICATE_NS=$t TWINS_DUPLICATE_AT_CLOSE=1 run equal-after
TWINS_DUPLICATE_NS=$((t+1)) run after
printf '0\n' >"$out/exit-status"
