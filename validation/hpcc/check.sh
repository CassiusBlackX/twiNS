#!/bin/bash
# Compare twins-rdma with rdma-ref on every job of a job file (lines "name topo flows [KEY=VALUE ...]").
# The references (name.fct, and name.tx with --tx) are read from $REF (default out/rdma/ref); topo
# and flow files are looked up in data/rdma, data/topo and data/flows.
#
# usage: validation/hpcc/check.sh JOBFILE [--tx]
cd "$(dirname "$0")/../.."
REF=${REF:-out/rdma/ref}
BIN=${BIN:-build/twins-rdma}
CFG=${CFG:-validation/hpcc/hpcc.cfg}
mkdir -p out/rdma/sim
find_file() {
    for d in data/rdma data/topo data/flows; do
        [ -f "$d/$1" ] && { echo "$d/$1"; return; }
    done
    echo "$1"
}
ok=0
n=0
while read -r name topo flows rest; do
    [ -z "$name" ] || [ "${name:0:1}" = "#" ] && continue
    [ -f "$REF/$name.fct" ] || { echo "$name: no reference"; continue; }
    n=$((n + 1))
    args=()
    [ "$2" = "--tx" ] && args+=(--txtrace=out/rdma/sim/$name.tx)
    t0=$(date +%s.%N)
    $BIN $CFG --topo=$(find_file $topo) --flows=$(find_file $flows) --out=out/rdma/sim/$name.fct "${args[@]}" $rest > out/rdma/sim/$name.log 2>&1
    rc=$?
    t1=$(date +%s.%N)
    total=$(wc -l < "$REF/$name.fct")
    same=$(join <(sort -k1,1 "$REF/$name.fct") <(sort -k1,1 out/rdma/sim/$name.fct) | awk '$2 == $4 && $3 == $5' | wc -l)
    info=$(grep -o "undecided ties [0-9]*, on lost tails [0-9]*\|pauses [0-9]*\|marks [0-9]*" out/rdma/sim/$name.log | tr '\n' ' ')
    printf "%-24s %6d flows, %6d identical  (%.2f s, rc %d) %s\n" "$name" "$total" "$same" "$(echo "$t1 - $t0" | bc)" $rc "$info"
    [ "$same" = "$total" ] && [ $rc = 0 ] && ok=$((ok + 1))
    if [ "$2" = "--tx" ] && [ -f "$REF/$name.tx" ] && [ "$same" != "$total" ]; then
        python3 validation/hpcc/txdiff.py "$REF/$name.tx" out/rdma/sim/$name.tx | head -12
    fi
done < "$1"
echo "$ok of $n runs identical"
[ $ok = $n ]
