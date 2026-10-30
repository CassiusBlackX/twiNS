#!/bin/bash
# check_whatif.sh [self|cross|closed] [CONFIG ...]: the what-if matrix (run_whatif.sh on a87; run.dag,
# run.ref of w_LOAD_CONFIG in out/simai/whatif/LOAD_CONFIG.*). self: each configuration's
# own DAG in twins-rdma, against SimAI on it. cross: the DAG recorded on sx replayed on the others.
# closed: no recording at all (twins-ai, the workloads in WL, default out/simai/wl), against SimAI.
# env: BIN (build/twins-rdma), CFG (validation/simai/SimAI.conf), W (out/simai/whatif), DATA (data/simai), TMO,
# NORUN=1: compare the runs already made
cd "$(dirname "$0")/../.."
ROOT=$PWD
AI=${AI:-build/twins-ai}
WL=${WL:-$ROOT/out/simai/wl}
BIN=${BIN:-build/twins-rdma}
CFG=${CFG:-validation/simai/SimAI.conf}
W=${W:-out/simai/whatif}
DATA=${DATA:-data/simai}
mode=${1:-self}
shift
CONFIGS=${*:-sx hpn dcn cc3 b2 g200}
for c in $CONFIGS; do
    for l in ${LOADS:-mar:128:microAR_tp128 moe:16:moe_16 ar64:128:paper/c_allreduce_64m_128 llp:128:paper/llama_65b_probe_128 a2a:128:paper/c_alltoall_64m_128}; do
        IFS=: read load g wl <<< "$l"
        wl=$wl.txt
        case $c in
        hpn | dcn) topo=$c$g.topo ;;
        dcnb*) topo=dcn$g.topo ;;
        g200) topo=sx${g}_200.topo ;;
        *) topo=sx$g.topo ;;
        esac
        case $c in
        cc3) rest=CC_MODE=3 ;;
        b[0-9]*) rest=BUFFER_SIZE=${c#b} ;;
        dcnb*) rest=BUFFER_SIZE=${c#dcnb} ;;
        *) rest= ;;
        esac
        rec=$c
        [ $mode = cross ] && rec=sx
        out=$W/$load.$c.$mode
        if [ $mode = closed ]; then
            # (SimAI's csv files in a directory of the run's)
            mkdir -p "$out.d"
            s=$(date +%s.%N)
            ( cd "$out.d" && timeout ${TMO:-1800} "$ROOT/$AI" "$ROOT/$CFG" --topo="$ROOT/$DATA/$topo" --astra="$WL/$wl" --out="$ROOT/$out" --dag-out="$ROOT/$out.dag" --flows-cache="$ROOT/$W/$load.flows.dag" $rest > "$ROOT/$out.log" 2>&1 )
            rc=$?
            rm -rf "$out.d"
            w=$(echo "$(date +%s.%N) - $s" | bc)
            [ $rc = 0 ] || { echo "$load $c: rc $rc: $(grep -am1 'error\|failed' "$out.log")"; continue; }
            python3 validation/simai/check_whatif.py "${load}_$c" "$W/${load}_$c.dag" "$W/${load}_$c.ref" "$out.dag" "$out" --lines
            echo "$(grep -a 'closed loop:' "$out.log" | sed 's/^closed loop: //') wall ${w} s; $(grep -a -o 'undecided ties [0-9]*, on lost tails [0-9]*' "$out.log" | tail -1)"
            continue
        fi
        if [ -z "$NORUN" ]; then
            timeout ${TMO:-1800} $BIN "$CFG" --topo="$DATA/$topo" --flows="$W/${load}_$rec.dag" --out="$out" $rest > "$out.log" 2>&1
            rc=$?
            [ $rc = 0 ] || { echo "$load $c: rc $rc: $(grep -m1 error: "$out.log")"; continue; }
        fi
        python3 validation/simai/check_whatif.py "${load}_$c" "$W/${load}_$c.dag" "$W/${load}_$c.ref" "$W/${load}_$rec.dag" "$out"
    done
done
