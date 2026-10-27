#!/bin/bash
# Run a job file as one batch of worlds (twins-rdma --batch) and compare every world with its
# reference in $REF (default out/rdma/ref).
#
# usage: validation/hpcc/check_batch.sh JOBFILE [twins-rdma options]
cd "$(dirname "$0")/../.."
REF=${REF:-out/rdma/ref}
BIN=${BIN:-build/twins-rdma}
CFG=${CFG:-validation/hpcc/hpcc.cfg}
OUT=out/rdma/batch
mkdir -p $OUT
jobs=$1
shift
grep -v "^#" "$jobs" | cut -d" " -f1 | grep -v "^$" | while read -r n; do rm -f $OUT/$n.fct; done
$BIN $CFG --batch="$jobs" --outdir=$OUT --path=data/rdma:data/topo:data/flows "$@" > $OUT/batch.log 2>&1
rc=$?
tail -3 $OUT/batch.log
ok=0
n=0
for name in $(grep -v "^#" "$jobs" | cut -d" " -f1 | grep -v "^$"); do
    n=$((n + 1))
    total=$(wc -l < "$REF/$name.fct")
    same=$(join <(sort -k1,1 "$REF/$name.fct") <(sort -k1,1 $OUT/$name.fct 2>/dev/null) | awk '$2 == $4 && $3 == $5' | wc -l)
    [ "$same" = "$total" ] && ok=$((ok + 1)) || echo "$name: $same of $total flows identical"
done
echo "$ok of $n worlds identical (rc $rc)"
[ $ok = $n ] && [ $rc = 0 ]
