#!/bin/bash
# The topologies and workloads of docs/topologies.md: about 128, 1024 and 8192 hosts on each
# topology (switches of at most 32 ports, TwiNS's TCP limit: two-tier leaf-spines stop at 768 hosts), with all-to-all web search at 30% (1 ms of arrivals), 16-to-1 incasts (10 rounds), and
# Facebook Hadoop at 30% (1 ms). Writes data/topo/<t>.txt and data/flows/<t>_<w>.csv (+ .csv.topo).
cd "$(dirname "$0")/.."
T=data/topo
F=data/flows
P="python3 workloads/topo.py"
G="python3 workloads/gen.py"
# name: topo.py arguments (jf256 exists already: jellyfish 256 12 4)
declare -A TOPO=(
    [ls8o]="leafspine 8 4 12" [t3d4]="torus3 4 4 4 2" [hx4]="hyperx 8 4 4" [xp4]="xpander 4 8 3 1"
    [ls32]="leafspine 32 16 16" [ls32o]="leafspine 32 8 24" [tor16]="torus 16 16 4"
    [t3d8]="torus3 8 8 8 2" [df8]="dragonfly 8 33 4 4" [hx12]="hyperx 7 12 12" [xp8]="xpander 8 28 4 1"
    [jf2048]="jellyfish2 2048 12 4 1"
    [tor32]="torus 32 32 8" [t3d16]="torus3 16 16 16 2" [df16]="dragonfly 16 65 8 8"
    [hx4d]="hyperx 8 4 4 8 8" [xp16]="xpander 16 120 4 1"
)
for t in "${!TOPO[@]}"; do [ -f $T/$t.txt ] || $P ${TOPO[$t]} > $T/$t.txt; done
for t in "${!TOPO[@]}" jf256; do
    h=$(sed -n 's/^hosts //p' $T/$t.txt)
    for w in a2a30 incast16 hdp30; do
        o=$F/${t}_$w.csv
        [ -f $o ] && continue
        case $w in
        a2a30) $G --hosts $h --pattern alltoall --size websearch --load 0.3 --duration_ns 1000000 --seed 1 --out $o ;;
        incast16) $G --hosts $h --pattern incast --load 0 --fanin 16 --incast_bytes 93440 --incast_period_ns 200000 \
            --duration_ns 2000000 --seed 1 --out $o ;;
        hdp30) $G --hosts $h --pattern alltoall --size cdf --cdf workloads/cdf/fb_hadoop.txt --align byte --load 0.3 \
            --duration_ns 1000000 --seed 1 --out $o ;;
        esac
        echo "# twins --topo=graph --topofile=$T/$t.txt" > $o.topo
    done
done
