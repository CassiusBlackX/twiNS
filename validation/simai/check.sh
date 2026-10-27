#!/bin/bash
# Compare twins-rdma with simai-ref on every job of a job file (lines "name topo flows [KEY=VALUE ...]"):
# every flow's start and completion (name.fct) and its first SendComplete (name.sent). The
# references are read from $REF (default out/simai/ref); topo and flow files are looked up in
# data/simai, data/rdma, data/topo and data/flows.
#
# usage: validation/simai/check.sh JOBFILE [--tx]
cd "$(dirname "$0")/../.."
REF=${REF:-out/simai/ref}
BIN=${BIN:-build/twins-rdma}
CFG=${CFG:-validation/simai/SimAI.conf}
OUT=out/simai/sim
mkdir -p $OUT
find_file() {
    for d in data/simai data/rdma data/topo data/flows; do
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
    [ "$2" = "--tx" ] && args+=(--txtrace=$OUT/$name.tx)
    t0=$(date +%s.%N)
    $BIN $CFG --topo=$(find_file $topo) --flows=$(find_file $flows) --out=$OUT/$name.fct --sent=$OUT/$name.sent "${args[@]}" $rest > $OUT/$name.log 2>&1
    rc=$?
    t1=$(date +%s.%N)
    total=$(wc -l < "$REF/$name.fct")
    same=$(join <(sort -k1,1 "$REF/$name.fct") <(sort -k1,1 $OUT/$name.fct) | awk '$2 == $4 && $3 == $5' | wc -l)
    stotal=$(wc -l < "$REF/$name.sent")
    ssame=$(join <(sort -k1,1 "$REF/$name.sent") <(sort -k1,1 $OUT/$name.sent) | awk '$2 == $3' | wc -l)
    info=$(grep -o "undecided ties [0-9]*, on lost tails [0-9]*\|pauses [0-9]*\|marks [0-9]*" $OUT/$name.log | tr '\n' ' ')
    printf "%-24s %6d flows, %6d identical, sent %6d/%-6d (%.2f s, rc %d) %s\n" "$name" "$total" "$same" "$ssame" "$stotal" "$(echo "$t1 - $t0" | bc)" $rc "$info"
    [ "$same" = "$total" ] && [ "$ssame" = "$stotal" ] && [ $rc = 0 ] && ok=$((ok + 1))
    if [ "$2" = "--tx" ] && [ -f "$REF/$name.tx" ] && [ "$same" != "$total" ]; then
        python3 validation/hpcc/txdiff.py "$REF/$name.tx" $OUT/$name.tx | head -12
    fi
done < "$1"
echo "$ok of $n runs identical"
[ $ok = $n ]
