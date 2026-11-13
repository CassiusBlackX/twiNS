#!/bin/bash
# Cost of exactness (TCP): build (chains) vs build-nochain (NS_NO_CHAIN: ties in arbitrary order),
# interleaved, min of N; with drops/retx (work done) and flows equal to ns-3.
cd "$(dirname "$0")/../.."
N=${N:-3}
for w in ${W:-k8_a2a30 k8_a2a50s k8_incast16 k16_paper k16_a2a30 k16_a2a50s}; do
    for t in ${T:-dctcp newreno}; do
        case $t in dctcp) TT=TcpDctcp ;; newreno) TT=TcpNewReno ;; cubic) TT=TcpCubic ;; esac
        k=$(echo $w | sed 's/k\([0-9]*\)_.*/\1/')
        declare -A best info same
        for i in $(seq $N); do
            for b in build build-nochain; do
                bin=$b/twins
                [ $b = build ] && [ -n "$(grep -l . /dev/null)" ] || true
                o=$(timeout 600 $bin --k=$k --flows=data/flows/$w.csv --out=out/audit/cost_${b}.csv --tcp=$t 2>&1)
                wall=$(echo "$o" | grep -o 'wall [0-9.]*' | awk '{print $2}')
                [ -z "${best[$b]}" ] || [ $(echo "$wall < ${best[$b]}" | bc) = 1 ] && best[$b]=$wall
                info[$b]=$(echo "$o" | grep -o 'drops [0-9]*\|retx [0-9]*\|undecided-ties [0-9]*' | tr '\n' ' ')
                same[$b]=$(paste -d, <(tail -n +2 data/ns3/ref/${w}_$TT.csv | cut -d, -f5,6) <(tail -n +2 out/audit/cost_${b}.csv | cut -d, -f5,6) | awk -F, '{n++; if ($1 == $3 && $2 == $4) x++} END {print x+0 "/" n}')
            done
        done
        echo "$w $t | chains ${best[build]} s, ${info[build]}, ns-3-equal ${same[build]} | no chains ${best[build-nochain]} s, ${info[build-nochain]}, ns-3-equal ${same[build-nochain]}"
        unset best info same
    done
done
