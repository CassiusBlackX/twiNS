#!/bin/bash
# check_closed.sh JOBFILE [KEY=VALUE ...]: twins-ai, without a recording, against SimAI end to end.
# Each line "name topo" of check_e2e.sh's job files is a traced SimAI run in E2E (NAME.dag and
# NAME.ref from trace2dag.py); twins-ai runs its workload (WL, the name without "_hpn"), and every
# message's start and completion, and every line of the DAG its system layer made, are compared.
# env: AI (build/twins-ai), CFG (validation/simai/SimAI.conf), E2E (out/simai/paper), DATA (data/simai),
# WL (out/simai/wl/paper), TMO (seconds per run, default 3 h)
cd "$(dirname "$0")/../.."
ROOT=$PWD
AI=${AI:-build/twins-ai}
CFG=${CFG:-validation/simai/SimAI.conf}
E2E=${E2E:-out/simai/paper}
DATA=${DATA:-data/simai}
WL=${WL:-out/simai/wl/paper}
jobs=$1
shift
ok=0
n=0
while read -r name topo rest; do
    case "$name" in "" | "#"*) continue ;; esac
    n=$((n + 1))
    out=$E2E/$name.closed
    d=$out.d
    mkdir -p "$d"
    s=$(date +%s.%N)
    ( cd "$d" && timeout "${TMO:-10800}" "$ROOT/$AI" "$ROOT/$CFG" --topo="$ROOT/$DATA/$topo" --astra="$ROOT/$WL/${name/_hpn/_}.txt" --out="$ROOT/$out" --dag-out="$ROOT/$out.dag" $rest "$@" > "$ROOT/$out.log" 2>&1 )
    rc=$?
    w=$(echo "$(date +%s.%N) - $s" | bc)
    rm -rf "$d"
    if [ $rc != 0 ]; then
        printf "%-30s rc %d: %s\n" "$name" "$rc" "$(grep -am1 'error\|failed\|more than\|overflowed' "$out.log")"
        continue
    fi
    if python3 validation/simai/compare_closed.py "$E2E/$name.ref" "$out" "$E2E/$name.dag" "$out.dag" >"$out.compare.json"; then
        ok=$((ok + 1))
        state=identical
    else
        state=different
    fi
    printf "%-30s %s; %.1f s; %s\n" "$name" "$state" "$w" "$(grep -a 'closed loop:' "$out.log" | tail -1 | sed 's/.*lead ties; //')"
done < "$jobs"
echo "$ok of $n runs identical"
[ "$ok" = "$n" ]
