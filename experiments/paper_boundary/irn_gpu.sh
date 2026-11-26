#!/usr/bin/env bash
set -euo pipefail
bin=$(realpath "$1")
inputs=$(realpath "$2")
d=$(realpath -m "$3")
mkdir "$d"
printf 'src,dst,flow_size,start_ns\n0,2,%s,2000000000\n1,2,4001,2000000040\n' "${TWINS_IRN_FIRST_SIZE:-3001}" > "$d/two.csv"
shift 3
if (( $# == 0 )); then set -- 100000 500 800 900; fi
for v in "$@"; do
 out="$d/$v"
 mkdir "$out"
 (cd "$out"
   rc=0
   timeout 60 "$bin" "$inputs/irn.cfg" "--flows=$d/two.csv" "--topo=$inputs/irn.graph" \
     --delay=1000 --out=flows.fct --txtrace=wire.raw --from=1999999000 --until=2000200000 \
     IRN_BDP=104000 "IRN_RTO_LOW_NS=$v" IRN_RTO_HIGH_NS=320000 "SIMULATOR_STOP_TIME=2.0001" \
     --fp-bases=1000003,1000033 >run.log 2>&1 || rc=$?
   printf '%s\n' "$rc" >rc
 )
done
