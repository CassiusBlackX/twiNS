#!/usr/bin/env bash
set -euo pipefail
bin=$(realpath "$1")
input=$(realpath "$2")
out=$(realpath -m "$3")
mkdir "$out"
cat >"$out/two.csv" <<'FLOW'
src,dst,flow_size,start_ns
0,2,970,2000000000
1,2,4001,2000000040
FLOW
run() (
    name=$1
    shift
    mkdir "$out/$name"
    cd "$out/$name"
    rc=0
    timeout 100 "$bin" "$input/irn.cfg" "--topo=$input/irn.graph" "--flows=$out/two.csv" --delay=1000 \
        --out=flows.fct --txtrace=wire.raw --from=1999999000 --until=2000099999 \
        IRN_BDP=104000 IRN_RTO_LOW_NS=834 IRN_RTO_HIGH_NS=320000 SIMULATOR_STOP_TIME=2.0001 \
        --fp-bases=1000003,1000033 "$@" >run.log 2>&1 || rc=$?
    printf '%s\n' "$rc" >rc
)
run default
run drain --drain
run cutoff-equal --stop-ns=2000004170
