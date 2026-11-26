#!/usr/bin/env bash
set -euo pipefail
bin=$(realpath "$1")
inputs=$(realpath "$2")
out=$(realpath -m "$3")
mkdir "$out"
sha256sum "$bin" > "$out/binary.sha256"
run() (
    name=$1 config=$2 graph=$3 flows=$4 stop=$5
    shift 5
    mkdir "$out/$name"
    cd "$out/$name"
    rc=0
    timeout 100 "$bin" "$config" "--topo=$graph" "--flows=$flows" --delay=1000 \
        --out=flows.fct --txtrace=wire.raw --from=1999999000 "--until=$((stop-1))" \
        "--stop-ns=$stop" IRN_BDP=104000 IRN_RTO_HIGH_NS=320000 \
        --fp-bases=1000003,1000033 "$@" >run.log 2>&1 || rc=$?
    printf '%s\n' "$rc" >rc
)
for group in original collision; do
    size=3001
    lows=(100000 500 800 900)
    if [[ $group == collision ]]; then size=970; lows=(833 834 835); fi
    flows="$out/${group}.csv"
    printf 'src,dst,flow_size,start_ns\n0,2,%s,2000000000\n1,2,4001,2000000040\n' "$size" >"$flows"
    for low in "${lows[@]}"; do
        run "$group-$low" "$inputs/irn.cfg" "$inputs/irn.graph" "$flows" 2000100000 "IRN_RTO_LOW_NS=$low"
    done
done
run crossing-834 "$inputs/irn.cfg" "$inputs/irn.graph" "$out/collision.csv" 2000004171 IRN_RTO_LOW_NS=834
python3 "$inputs/dense.py" "$out/dense-inputs" "$inputs"
dense="$out/dense-inputs"
for count in 63 64 65; do
    run "dense-$count-fixed" "$dense/$count/config" "$dense/graph" "$dense/$count/flows.csv" 2000100000 \
        IRN_RTO_LOW_NS=1000 --rate=1000Gbps --due-report=due.txt --due-cap=64
done
run dense-65-auto "$dense/65/config" "$dense/graph" "$dense/65/flows.csv" 2000100000 \
    IRN_RTO_LOW_NS=1000 --rate=1000Gbps --due-report=due.txt
