#!/bin/bash
# TwiNS against the stored ns-3 references (data/ns3/ref): for each workload and TCP, the share of
# flows whose establishment and completion times equal ns-3's.
# usage: validation/check_exact.sh "<workloads>" "<tcps>" [variant] [extra twins args]
#   workloads: e.g. "k8_paper k8_a2a30" (the k is taken from the name; ls_* = leaf-spine, whose
#              topology is in data/flows/<w>.csv.topo)
#   tcps: dctcp newreno cubic; variant: base (default) or jit, jit1, ...
cd "$(dirname "$0")/.."
FL=data/flows
REF=data/ns3/ref
OUT=out/nsm
mkdir -p $OUT
V=${3:-base}
EXTRA=${4:-}
for w in $1; do
    for t in $2; do
        case $t in
        dctcp) T=TcpDctcp ;;
        newreno) T=TcpNewReno ;;
        cubic) T=TcpCubic ;;
        esac
        if [ "$V" = base ]; then fl=$FL/$w.csv; rf=$REF/${w}_$T.csv; else fl=$FL/${w}_$V.csv; rf=$REF/${w}_${T}_$V.csv; fi
        case $w in
        ls_*) topo=$(sed -n 's/^# twins //p' $FL/$w.csv.topo 2>/dev/null) ;;
        *) topo="--k=$(echo $w | sed 's/k\([0-9]*\)_.*/\1/')" ;;
        esac
        out=$OUT/${w}_${t}_$V.csv
        ./build/twins $topo --flows=$fl --out=$out --tcp=$t --ecmp_from=$rf $EXTRA > /dev/null
        paste -d, <(tail -n +2 $rf | cut -d, -f5,6) <(tail -n +2 $out | cut -d, -f5,6) |
            awk -F, -v w=$w -v t=$t -v v=$V '{n++; if ($1 == $3 && $2 == $4) x++} END {printf "%s %s %s: %d/%d flows identical\n", w, t, v, x, n}'
    done
done
