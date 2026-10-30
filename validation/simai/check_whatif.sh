#!/bin/bash
# check_whatif.sh [self|cross] [CONFIG ...]: the what-if matrix (run_whatif.sh on a87; run.dag,
# run.ref of w_LOAD_CONFIG in out/simai/whatif/LOAD_CONFIG.*). self: each configuration's
# own DAG in twins-rdma, against SimAI on it. cross: the DAG recorded on sx replayed on the others.
# env: BIN (build/twins-rdma), CFG (validation/simai/SimAI.conf), W (out/simai/whatif), DATA (data/simai), TMO,
# NORUN=1: compare the runs already made
cd "$(dirname "$0")/../.."
BIN=${BIN:-build/twins-rdma}
CFG=${CFG:-validation/simai/SimAI.conf}
W=${W:-out/simai/whatif}
DATA=${DATA:-data/simai}
mode=${1:-self}
shift
CONFIGS=${*:-sx hpn dcn cc3 b2 g200}
for c in $CONFIGS; do
    for l in mar:128 moe:16 ar64:128 llp:128; do
        load=${l%:*} g=${l#*:}
        case $c in
        hpn | dcn) topo=$c$g.topo ;;
        g200) topo=sx${g}_200.topo ;;
        *) topo=sx$g.topo ;;
        esac
        case $c in
        cc3) rest=CC_MODE=3 ;;
        b[0-9]*) rest=BUFFER_SIZE=${c#b} ;;
        *) rest= ;;
        esac
        rec=$c
        [ $mode = cross ] && rec=sx
        out=$W/$load.$c.$mode
        if [ -z "$NORUN" ]; then
            timeout ${TMO:-1800} $BIN "$CFG" --topo="$DATA/$topo" --flows="$W/${load}_$rec.dag" --out="$out" $rest > "$out.log" 2>&1
            rc=$?
            [ $rc = 0 ] || { echo "$load $c: rc $rc: $(grep -m1 error: "$out.log")"; continue; }
        fi
        python3 validation/simai/check_whatif.py "${load}_$c" "$W/${load}_$c.dag" "$W/${load}_$c.ref" "$W/${load}_$rec.dag" "$out"
    done
done
