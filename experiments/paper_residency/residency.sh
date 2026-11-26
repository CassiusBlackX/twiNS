#!/usr/bin/env bash
set -euo pipefail
bin=$(realpath "$1")
analyze=$(realpath "$2")
inputs=$(realpath "$3")
out=$(realpath -m "$4")
mkdir "$out"
for sack in 0 1; do
    (
        mkdir "$out/$sack"
        cd "$out/$sack"
        TWINS_TCP_RESIDENCY_FILE=arrivals.bin "$bin" --single-pass --tcp=dctcp --queue=200 --spray=0 \
          "--tcp-sack=$sack" --tcp-timestamps=1 --topo=graph "--topofile=$inputs/1024.graph" \
          --graph_k=16 "--flows=$inputs/1024.csv" --out=flows.csv >run.log 2>&1
        frame=$(sed -n 's/^Arrival audit: .*frame_bytes=\([0-9]*\) slot_ps=.*/\1/p' run.log)
        slot=$(sed -n 's/^Arrival audit: .*slot_ps=\([0-9]*\)$/\1/p' run.log)
        "$analyze" arrivals.bin "$slot" "$frame" >residency.json
        sha256sum arrivals.bin flows.csv >outputs.sha256
    )
done
