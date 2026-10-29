#!/bin/bash
# run_paper.sh MEM TMO NAME:TOPO:WORKLOAD ...: traced SimAI runs (simai-trace, sequential) of the
# paper's workloads (gen_paper.sh), all at once, each in a container capped at MEM with a time
# limit of TMO seconds; outputs in ~/netsim/aiwork/runs/NAME (fct.txt, trace.txt, wall). Start it
# with nohup: it outlives the ssh session. V=seq (or mtp, with THR threads) for untraced timing
# runs; the traced runs use simai-trace64 (trace_patch.py with 64-bit event uids; TV=trace for the
# 32-bit one, whose uids wrap after 2^32 events).
cd ~/netsim/aiwork
mem=$1 tmo=$2; shift 2
for j in "$@"; do
    IFS=: read name topo wl <<< "$j"
    if [ -z "$V" ]; then
        ( V=${TV:-trace64} MEM=$mem TMO=$tmo ./run_simai.sh $name $topo paper/$wl.txt 1 TWINS_TRACE=/work/runs/$name/trace.txt
          echo "$(date +%T) $name $(cat runs/$name/wall) $(grep -c . runs/$name/fct.txt 2>/dev/null) flows" ) &
    else
        ( V=$V MEM=$mem TMO=$tmo ./run_simai.sh $name $topo paper/$wl.txt ${THR:-1}
          echo "$(date +%T) $name $(cat runs/$name/wall) $(grep -c . runs/$name/fct.txt 2>/dev/null) flows" ) &
    fi
done
wait
echo "$(date +%T) ALLDONE"
