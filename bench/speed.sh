#!/bin/bash
# Wall time of TwiNS (min of N runs) on the validation workloads.
# usage: bench/ns_speed.sh [N] [workload...]   (workloads: k8_paper k16_a2a50s ...)
cd "$(dirname "$0")/.."
N=${1:-3}
shift
W=${*:-"k8_paper k8_fullsync k8_fullexp k8_a2a30 k8_incast16 k8_a2a50s k16_paper k16_a2a30 k16_incast16 k16_a2a50s k32_paper"}
R=data/flows
OUT=$(mktemp)
echo "workload,tcp,flows,slots,wall_s"
for w in $W; do
    k=$(echo $w | sed 's/k\([0-9]*\)_.*/\1/')
    for t in dctcp newreno; do
        best=""
        for i in $(seq $N); do
            r=$(./build/twins --k=$k --flows=$R/$w.csv --out=$OUT --tcp=$t)
            v=$(echo "$r" | tail -1 | grep -o 'wall [0-9.]*' | awk '{print $2}')
            best=$(python3 -c "print(min(float('$v'), float('${best:-1e9}')))")
            slots=$(echo "$r" | grep -o 'Executed Slots: [0-9]*' | awk '{print $3}')
            flows=$(echo "$r" | tail -1 | grep -o 'flows [0-9]*' | awk '{print $2}')
        done
        echo "$w,$t,$flows,$slots,$best"
    done
done
rm -f $OUT
