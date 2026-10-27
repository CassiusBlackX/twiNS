#!/bin/bash
# Reruns both parameter sweeps with TwiNS (batches of worlds) and compares every configuration
# with the stored ns-3 outputs (data/ns3/sweep_k8, data/ns3/sweep_k16).
# usage: validation/check_sweeps.sh [k8] [k16]   (default: both)
cd "$(dirname "$0")/.."
cmp_sweep() {
    local n=0 x=0
    for f in $1/*.csv; do
        b=$(basename $f)
        n=$((n + 1))
        if cmp -s <(cut -d, -f1-6 $f) <(cut -d, -f1-6 $2/$b); then x=$((x + 1)); else echo "differs: $b"; fi
    done
    echo "$x of $n configurations identical to ns-3"
}
for s in ${*:-k8 k16}; do
    case $s in
    k8)
        python3 bench/sweep.py --out out/sweep_k8 --batch 69 --hashes "data/ns3/sweep_k8/{w}_TcpDctcp_q200_K30.csv" | tail -1
        cmp_sweep data/ns3/sweep_k8 out/sweep_k8 ;;
    k16)
        python3 bench/sweep.py --k 16 --workloads k16_a2a30,k16_a2a50w --queues 100,400 --Ks 10,20,40,80 --batch 16 \
            --out out/sweep_k16 --hashes "data/ns3/sweep_k16/{w}_TcpDctcp_q100_K20.csv" | tail -1
        cmp_sweep data/ns3/sweep_k16 out/sweep_k16 ;;
    esac
done
