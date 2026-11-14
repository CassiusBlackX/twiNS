#!/bin/bash
# The randomized grid (gen_grid.py) against its ns-3 references in data/ns3/grid (run_jobs.sh's ref/):
# for each scenario, the flows whose establishment and completion times equal ns-3's, and the run's
# flags (undecided ties, then the 128-byte build; capacity overflows).
# usage: validation/grid/check_grid.sh [JOBS]   env: BIN (build/twins), BIN128 (build/twins128)
cd "$(dirname "$0")/../.."
J=${1:-validation/grid/jobs_twins.txt}
BIN=${BIN:-build/twins}
BIN128=${BIN128:-build/twins128}
REF=data/ns3/grid
OUT=out/grid
mkdir -p $OUT
runs=0 same_runs=0 flows=0 same_flows=0 r128=0 flagged=0
while read -r name args; do
    case "$name" in "" | "#"*) continue ;; esac
    rf=$REF/$name.csv
    [ -f "$rf" ] || { echo "$name: no reference"; continue; }
    out=$OUT/$name.csv
    b=twins
    o=$(timeout 600 $BIN $args --out=$out 2>&1)
    if echo "$o" | grep -q "were undecided"; then
        b=twins128
        r128=$((r128 + 1))
        o=$(timeout 600 $BIN128 $args --out=$out 2>&1)
    fi
    flag=""
    echo "$o" | grep -q "were undecided" && flag="$flag undecided"
    echo "$o" | grep -q "a capacity overflowed" && flag="$flag overflow($(echo "$o" | grep -o '[a-z-]*-ovf [1-9][0-9]*\|tx-cap [1-9][0-9]*' | tr '\n' ' '))"
    [ -n "$flag" ] && flagged=$((flagged + 1))
    r=$(paste -d, <(tail -n +2 $rf | cut -d, -f5,6) <(tail -n +2 $out | cut -d, -f5,6) | awk -F, '{n++; if ($1 == $3 && $2 == $4) x++} END {print n + 0, x + 0}')
    set -- $r
    runs=$((runs + 1)); flows=$((flows + $1)); same_flows=$((same_flows + $2))
    [ "$1" = "$2" ] && same_runs=$((same_runs + 1))
    printf "%s %d/%d flows identical (%s)%s\n" "$name" "$2" "$1" "$b" "${flag:+, flags:$flag}"
done < "$J"
echo "grid: $same_runs of $runs runs identical; $same_flows of $flows flows; $r128 reran with twins128; $flagged runs with flags left"
