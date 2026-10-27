#!/bin/bash
# Runs ns-3 reference jobs in parallel. Each line of the job file is
#   <output name> <twins-ref arguments...>
# and writes ref/<output name>.csv (skipped if it exists), with the log next to it.
# usage: run_jobs.sh <job file> [parallel runs, default 32]
cd "${NS3_WORK:-$HOME/netsim/cmp}"
B=${NS3_BIN:-../unison/build/scratch/ns3.46.1-twins-ref-optimized}
mkdir -p ref
one() {
    local o=ref/$1.csv
    shift
    [ -f $o ] && return
    $B "$@" --out=$o.tmp > $o.log 2>&1 && mv $o.tmp $o
    echo "$(date +%T) $o $(tail -1 $o.log | cut -c1-100)"
}
export -f one
export B
s=$(date +%s.%N)
grep -v '^#' "$1" | grep . | xargs -P ${2:-32} -L 1 bash -c 'one "$@"' _
e=$(date +%s.%N)
echo "$(grep -v '^#' "$1" | grep -c .) jobs, wall $(python3 -c "print(round($e-$s,1))") s"
echo ALL DONE
