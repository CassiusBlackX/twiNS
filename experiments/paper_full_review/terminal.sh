#!/usr/bin/env bash
set -u
root=$(pwd -P)
out=$root/out/full-review-20261126
inputs=$out/stage/inputs
result=$out/hip-terminal
mkdir -p "$result"
printf 'src,dst,flow_size,start_ns\n0,1,10,1000\n' >"$result/one.csv"
run() {
    local name=$1
    shift
    mkdir "$result/$name"
    (cd "$result/$name" && timeout 90s "$@" >run.log 2>&1
     printf '%s\n' "$?" >rc)
}
tcp=$out/hip-bounded/twins
common=(--k=4 --tcp=newreno --queue=1024)
run tcp-clean "$tcp" "${common[@]}" "--flows=$result/one.csv" --out=flows.csv
run tcp-full "$tcp" "${common[@]}" "--flows=$root/out/tcp-options/inputs/capacity.csv" --out=flows.csv
rdma=$out/hip-build/twins-rdma
common=("$inputs/hpcc.cfg" "--topo=$inputs/fattree8.txt" "--flows=$inputs/k8_a2a30.csv" --out=output.fct CC_MODE=1)
run rdma-clean "$rdma" "${common[@]}"
run rdma-frame-fixed "$rdma" "${common[@]}" --fcap=1
run rdma-frame-auto "$rdma" "${common[@]}" --fcap-auto=1
run rdma-tail-fixed "$rdma" "${common[@]}" --tail=8
run rdma-q3-fixed "$rdma" "${common[@]}" --q3cap=1
