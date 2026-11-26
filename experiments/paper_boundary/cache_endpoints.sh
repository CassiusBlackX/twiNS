#!/usr/bin/env bash
set -euo pipefail
b=$(realpath "$1")
i=$(realpath "$2")
o=$(realpath -m "$3")
mkdir "$o"
sed 's/8388608/1048576/' "$i/workload.log" >"$o/original.wl"
sed 's/model_parallel_NPU_group: 8/model_parallel_NPU_group: 4/' "$o/original.wl" >"$o/changed.wl"
run() {
 local name=$1
 local wl=$2
 shift 2
 mkdir "$o/$name"
 (cd "$o/$name"
  timeout 55 "$b" "$i/config.log" "--topo=$i/topo.log" "--astra=$wl" --out=messages.fct --dag-out=actions.dag \
    --nvl-islands=0 --gwin=0 --fp-bases=1000003,1000033 BUFFER_SIZE=64 "$@" >run.log 2>&1
 )
}
run original "$o/original.wl" "--flows-cache=$o/cache.dag"
cp "$o/cache.dag" "$o/original.dag"
run changed "$o/changed.wl" "--flows-cache=$o/cache.dag"
run cold "$o/changed.wl"
cmp "$o/changed/messages.fct" "$o/cold/messages.fct"
cmp "$o/changed/actions.dag" "$o/cold/actions.dag"
! grep -q ', kept' "$o/changed/run.log"
awk '$1=="flow" {print $3,$4}' "$o/original.dag" | LC_ALL=C sort -u >"$o/original.endpoints"
awk '$1=="flow" {print $3,$4}' "$o/cache.dag" | LC_ALL=C sort -u >"$o/changed.endpoints"
! cmp -s "$o/original.endpoints" "$o/changed.endpoints"
printf '0\n' >"$o/rc"
