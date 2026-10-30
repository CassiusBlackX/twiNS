#!/bin/bash
# TwiNS against the stored ns-3 references (data/ns3/ref): for each workload and TCP, the share of
# flows whose establishment and completion times equal ns-3's.
# usage: validation/check_exact.sh "<workloads>" "<tcps>" [variant] [extra twins args]
#   workloads: e.g. "k8_paper k8_a2a30" (the FatTree k is taken from the name, unless
#              data/flows/<w>.csv.topo gives the topology arguments: leaf-spine, topology files)
#   tcps: dctcp newreno cubic; variant: base (default), jit, jit1, ... (start-jittered flow files)
#   or, with the base flows: fin (senders closing their connections), spray (per-packet ECMP),
#   sprayfin (both)
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
        X=$EXTRA
        if [ "$V" = base ]; then
            fl=$FL/$w.csv; rf=$REF/${w}_$T.csv
        elif [ "$V" = fin ]; then
            fl=$FL/$w.csv; rf=$REF/${w}_${T}_fin.csv; X="$X --fin=1" # senders close (FIN)
        elif [ "$V" = spray ]; then
            fl=$FL/$w.csv; rf=$REF/${w}_${T}_spray.csv; X="$X --spray=1" # per-packet ECMP
        elif [ "$V" = sprayfin ]; then
            fl=$FL/$w.csv; rf=$REF/${w}_${T}_sprayfin.csv; X="$X --spray=1 --fin=1"
        else
            fl=$FL/${w}_$V.csv; rf=$REF/${w}_${T}_$V.csv
        fi
        if [ -f $FL/$w.csv.topo ]; then
            topo=$(sed -n 's/^# twins //p' $FL/$w.csv.topo) # leaf-spine, topology files
        else
            topo="--k=$(echo $w | sed 's/k\([0-9]*\)_.*/\1/')"
        fi
        out=$OUT/${w}_${t}_$V.csv
        bin=twins
        # (TwiNS computes ns-3's ECMP hashes; ECMP_FROM=1 takes them from the reference instead)
        [ -n "$ECMP_FROM" ] && X="$X --ecmp_from=$rf"
        if ./build/twins $topo --flows=$fl --out=$out --tcp=$t $X 2>&1 > /dev/null | grep -q undecided; then
            bin=twins128 # undecided ties: longer event histories
            ./build/twins128 $topo --flows=$fl --out=$out --tcp=$t $X > /dev/null 2>&1
        fi
        paste -d, <(tail -n +2 $rf | cut -d, -f5,6) <(tail -n +2 $out | cut -d, -f5,6) |
            awk -F, -v w=$w -v t=$t -v v=$V -v b=$bin '{n++; if ($1 == $3 && $2 == $4) x++} END {printf "%s %s %s: %d/%d flows identical%s\n", w, t, v, x, n, b == "twins" ? "" : " (twins128)"}'
    done
done
