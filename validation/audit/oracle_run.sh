#!/bin/bash
# on s50i: for each job "name args": run patched ns-3 with NS3_EVLOG, the oracle, delete the log
cd ~/Videos/netsim/evwork
B=../unison-evlog/build/scratch/ns3.46.1-twins-ref-optimized
one() {
    local n=$1; shift
    [ -f res/$n.txt ] && return
    ( cd ../cmp && NS3_EVLOG=$HOME/Videos/netsim/evwork/logs/$n.bin timeout 3600 $B "$@" --out=$HOME/Videos/netsim/evwork/logs/$n.csv > /dev/null 2>&1 )
    timeout 3600 ../oracle9 logs/$n.bin > res/$n.tmp 2>&1 && mv res/$n.tmp res/$n.txt
    rm -f logs/$n.bin
    echo "$(date +%T) $n done"
}
export -f one; export B
mkdir -p res logs
grep -v '^#' "$1" | grep . | xargs -P ${2:-4} -L 1 bash -c 'one "$@"' _
echo ALL DONE
