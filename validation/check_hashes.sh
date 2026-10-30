#!/bin/bash
# check_hashes.sh: TwiNS's ECMP flow hashes (computed as ns-3 does: src/main.cu) against those every
# stored ns-3 reference recorded (data/ns3/ref, columns hash_data and hash_ack), without simulating.
cd "$(dirname "$0")/.."
REF=data/ns3/ref
FL=data/flows
ok=0
n=0
for rf in $REF/*.csv; do
    b=$(basename $rf .csv)
    w=${b%%_Tcp*}
    rest=${b#*_Tcp}
    v=$(echo "$rest" | sed -n 's/^[A-Za-z]*_//p')
    case "$v" in
    "" | fin | spray | sprayfin | unison*) fl=$FL/$w.csv ;; # (unisonN: the base flows on Unison)
    *) fl=$FL/${w}_$v.csv ;;
    esac
    if [ -f $FL/$w.csv.topo ]; then
        topo=$(sed -n 's/^# twins //p' $FL/$w.csv.topo)
    else
        topo="--k=$(echo $w | sed 's/k\([0-9]*\)_.*/\1/')"
    fi
    n=$((n + 1))
    r=$(./build/twins $topo --flows=$fl --ecmp_from=$rf --hash_check=1 2>&1 | tail -1)
    case "$r" in
    *"ECMP hashes"*) ;;
    *) r="error: $r" ;;
    esac
    echo "$b: $r"
    [ "$(echo "$r" | awk '{print ($3 == $5) ? 1 : 0}')" = 1 ] && case "$r" in error*) ;; *) ok=$((ok + 1)) ;; esac
done
echo "$ok of $n references: every connection's hashes as ns-3's"
