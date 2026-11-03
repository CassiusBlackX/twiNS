#!/bin/bash
# TwiNS's event-loop time (min of N runs) on the topology files of docs/topologies.md.
# usage: bench/topo_speed.sh N "<topologies>" "<workloads>" "<tcps>"   (CSV on stdout)
cd "$(dirname "$0")/.."
N=${1:-3}
OUT=$(mktemp)
echo "topo,workload,tcp,flows,wall_s"
for t in $2; do
    for w in $3; do
        for tcp in $4; do
            best=""
            for i in $(seq $N); do
                r=$(timeout 900 ./build/twins --topo=graph --topofile=data/topo/$t.txt --flows=data/flows/${t}_$w.csv --out=$OUT --tcp=$tcp)
                v=$(echo "$r" | tail -1 | grep -o 'wall [0-9.]*' | awk '{print $2}')
                [ -n "$v" ] && best=$(python3 -c "print(min(float('$v'), float('${best:-1e9}')))")
                flows=$(echo "$r" | tail -1 | grep -o 'flows [0-9]*' | awk '{print $2}')
            done
            echo "$t,$w,$tcp,$flows,$best"
        done
    done
done
rm -f $OUT
