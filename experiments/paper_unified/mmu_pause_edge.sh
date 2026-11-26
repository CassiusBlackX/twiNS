#!/usr/bin/env bash
set -euo pipefail
backend=$1
binary=$(realpath "$2")
cfg=$(realpath "$3")
out=$(realpath -m "$4")
mkdir "$out"
cat >"$out/topology" <<'TOPO'
5 1 0 1 4 H100
4
0 4 1Gbps 680ns 0
1 4 10Mbps 1000ns 0
2 4 100Gbps 13725ns 0
3 4 100Gbps 13725ns 0
TOPO
for shared in 283 284 285; do
    mkdir "$out/$shared"
    (
        cd "$out/$shared"
        # Frames have a 94-byte modeled header. One full frame leaves
        # immediately and its successor cannot leave before the cutoff.
        size=$((22 * 106 + 86 + shared - 284))
        printf 'src,dst,flow_size,start_ns\n0,1,%s,0\n' "$size" >flows.csv
        for mode in normal serial; do
            [[ $backend != native || $mode != serial ]] || continue
            opts=()
            [[ $mode != serial ]] || opts+=(--serial)
            [[ $backend == native ]] || opts+=(--txtrace-cap=8192)
            rc=0
            TWINS_MMU_SNAPSHOT="$mode.boundaries" TWINS_MMU_STEP=680 timeout 30 "$binary" "$cfg" "--topo=$out/topology" \
                --flows=flows.csv --until=50000 "--out=$mode.fct" "--txtrace=$mode.wire" "${opts[@]}"                 CC_MODE=3 HAS_WIN=0 PACKET_PAYLOAD_SIZE=106 ENABLE_QCN=0 BUFFER_SIZE=1 SIMULATOR_STOP_TIME=0.00005 \
                KMIN_MAP=3,10000000,100,1000000000,100,100000000000,400 \
                KMAX_MAP=3,10000000,400,1000000000,400,100000000000,1600 \
                PMAX_MAP=3,10000000,0.2,1000000000,0.2,100000000000,0.2 >"$mode.log" 2>&1 || rc=$?
            printf '%s\n' "$rc" >"$mode.rc"
        done
    )
done
