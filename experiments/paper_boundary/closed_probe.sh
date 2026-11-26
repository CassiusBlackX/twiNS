#!/usr/bin/env bash
set -euo pipefail
bin=$(realpath "$1")
inputs=$(realpath "$2")
workload=$(realpath "$3")
d=$(realpath -m "$4")
mkdir "$d"
run() {
 local name=$1
 mkdir "$d/$name"
 (cd "$d/$name"
   rc=0
   timeout 90 "$bin" "$inputs/config.log" "--topo=$inputs/topo.log" "--astra=$workload" --out=messages.fct \
     --dag-out=actions.dag --txtrace=wire.raw --until=1000000 --nvl-islands=0 --gwin=0 --fp-bases=1000003,1000033 \
     BUFFER_SIZE=64 >run.log 2>&1 || rc=$?
   printf '%s\n' "$rc" >rc
 )
}
run control
TWINS_DUPLICATE_NS=44572 run before
TWINS_DUPLICATE_NS=44573 run equal-before
TWINS_DUPLICATE_NS=44573 TWINS_DUPLICATE_AT_CLOSE=1 run equal-after
TWINS_DUPLICATE_NS=44574 run after
