#!/bin/bash
# check_e2e.sh JOBFILE: TwiNS against SimAI end to end. Each line "name topo" is a traced SimAI run
# (validation/simai/trace_patch.py; run_e2e.sh on a87) whose trace and fct.txt are in
# out/simai/e2e as name.trace and name.fct. Its DAG (workloads/trace2dag.py) runs in twins-rdma,
# and every message's start and completion are compared with SimAI's.
# env: BIN (build/twins-rdma), CFG (validation/simai/SimAI.conf), E2E (out/simai/e2e), DATA (data/simai)
cd "$(dirname "$0")/../.."
BIN=${BIN:-build/twins-rdma}
CFG=${CFG:-validation/simai/SimAI.conf}
E2E=${E2E:-out/simai/e2e}
DATA=${DATA:-data/simai}
ok=0
n=0
while read -r name topo rest; do
    case "$name" in "" | "#"*) continue ;; esac
    n=$((n + 1))
    python3 workloads/trace2dag.py "$E2E/$name.trace" "$E2E/$name.fct" "$E2E/$name.dag" "$E2E/$name.ref" > /dev/null || { echo "$name: no DAG"; continue; }
    s=$(date +%s.%N)
    $BIN "$CFG" --topo="$DATA/$topo" --flows="$E2E/$name.dag" --out="$E2E/$name.twins" $rest > "$E2E/$name.twins.log" 2>&1
    rc=$?
    w=$(echo "$(date +%s.%N) - $s" | bc)
    r=$(join <(sort -k1,1 "$E2E/$name.ref") <(sort -k1,1 "$E2E/$name.twins") |
        awk '{ if ($2 == $4 && $3 == $5) s++; else d++ } END { printf "%d %d", s, d + 0 }')
    set -- $r
    total=$(wc -l < "$E2E/$name.ref")
    printf "%-14s %7d messages, %7d identical, %d differ (%.2f s, rc %d)\n" "$name" "$total" "$1" "$2" "$w" "$rc"
    [ "$1" = "$total" ] && [ "$2" = 0 ] && [ $rc = 0 ] && ok=$((ok + 1))
done < "$1"
echo "$ok of $n runs identical"
