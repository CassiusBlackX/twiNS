#!/bin/bash
# Negative control for exact-or-flag (TCP): run a TwiNS build with shortened chains (BIN, e.g.
# build-r1/twins, CHAIN_RUNS=1) on stored ns-3 references, without the twins128 rerun, and report
# per run: undecided ties (flag), flows identical. Exact-or-flag predicts that every run whose
# output differs from ns-3 has a flag.
# usage: BIN=build-r1/twins validation/audit/negctl_tcp.sh "<workloads>" "<tcps>" [variant] > out.txt
cd "$(dirname "$0")/../.."
BIN=${BIN:?BIN}
FL=data/flows
REF=data/ns3/ref
OUT=out/audit/negctl
mkdir -p $OUT
V=${3:-base}
for w in $1; do
    for t in $2; do
        case $t in dctcp) T=TcpDctcp ;; newreno) T=TcpNewReno ;; cubic) T=TcpCubic ;; esac
        if [ "$V" = base ]; then fl=$FL/$w.csv; rf=$REF/${w}_$T.csv; else fl=$FL/${w}_$V.csv; rf=$REF/${w}_${T}_$V.csv; fi
        [ -f "$rf" ] || continue
        if [ -f $FL/$w.csv.topo ]; then topo=$(sed -n 's/^# twins //p' $FL/$w.csv.topo); else topo="--k=$(echo $w | sed 's/k\([0-9]*\)_.*/\1/')"; fi
        out=$OUT/$(basename $BIN)_$(basename $(dirname $BIN))_${w}_${t}_$V.csv
        o=$(timeout 600 $BIN $topo --flows=$fl --out=$out --tcp=$t 2>&1)
        ties=$(echo "$o" | grep -o 'undecided-ties [0-9]*' | awk '{print $2}')
        r=$(paste -d, <(tail -n +2 $rf | cut -d, -f5,6) <(tail -n +2 $out | cut -d, -f5,6) | awk -F, '{n++; if ($1 == $3 && $2 == $4) x++} END {print x+0, n+0}')
        echo "$w $t $V ties ${ties:-NA} identical $r"
    done
done
