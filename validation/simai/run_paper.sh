#!/bin/bash
# run_paper.sh MEM TMO NAME:TOPO:WORKLOAD ...: traced SimAI runs (simai-trace, sequential) of the
# paper's workloads (gen_paper.sh), all at once, each in a container capped at MEM with a time
# limit of TMO seconds; outputs in ~/netsim/aiwork/runs/NAME (fct.txt, trace.txt, wall). Start it
# with nohup: it outlives the ssh session. V=ref for untraced timing runs.
cd ~/netsim/aiwork
mem=$1 tmo=$2; shift 2
for j in "$@"; do
    IFS=: read name topo wl <<< "$j"
    if [ "${V:-trace}" = trace ]; then
        ( V=trace MEM=$mem TMO=$tmo ./run_simai.sh $name $topo paper/$wl.txt 1 TWINS_TRACE=/work/runs/$name/trace.txt
          echo "$(date +%T) $name $(cat runs/$name/wall) $(grep -c . runs/$name/fct.txt 2>/dev/null) flows" ) &
    else
        ( V=$V MEM=$mem TMO=$tmo ./run_simai.sh $name $topo paper/$wl.txt ${THR:-1}
          echo "$(date +%T) $name $(cat runs/$name/wall) $(grep -c . runs/$name/fct.txt 2>/dev/null) flows" ) &
    fi
done
wait
echo "$(date +%T) ALLDONE"
