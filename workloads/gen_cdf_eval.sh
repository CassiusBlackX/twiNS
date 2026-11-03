#!/bin/bash
# The workloads of docs/workloads.md: Poisson arrivals with real flow-size CDFs (workloads/cdf/) at
# 30, 50 and 70% of each host's 100G, all-to-all, on FatTree k=8 (10 ms of arrivals) and k=16 (2 ms);
# and GeDES's pairing (host i with i + N/2, one connection each way, MSS-sized flows) for GeDES.
cd "$(dirname "$0")/.."
F=data/flows
G="python3 workloads/gen.py"
declare -A CDF=([ws]=websearch [hdp]=fb_hadoop [luna]=luna_write [dm]=datamining)
for c in ws hdp luna dm; do
    for l in 30 50 70; do
        o=$F/k8_${c}$l.csv
        [ -f $o ] || $G --k 8 --pattern alltoall --size cdf --cdf workloads/cdf/${CDF[$c]}.txt --align byte \
            --load 0.$l --duration_ns 10000000 --seed 1 --out $o
        o=$F/k16_${c}$l.csv
        [ -f $o ] || $G --k 16 --pattern alltoall --size cdf --cdf workloads/cdf/${CDF[$c]}.txt --align byte \
            --load 0.$l --duration_ns 2000000 --seed 1 --out $o
    done
done
# GeDES's pairs: web search over 10 ms at k=8 and 2 ms beyond, Hadoop over 1.5 ms (GeDES holds at
# most 99 flows a connection)
for k in 8 16 32; do
    o=$F/k${k}_pws50.csv
    [ -f $o ] || $G --k $k --pattern pair --arrival poisson --size cdf --cdf workloads/cdf/websearch.txt --align mss \
        --load 0.5 --duration_ns $([ $k = 8 ] && echo 10000000 || echo 2000000) --seed 1 --out $o
    o=$F/k${k}_phdp50.csv
    [ -f $o ] || $G --k $k --pattern pair --arrival poisson --size cdf --cdf workloads/cdf/fb_hadoop.txt --align mss \
        --load 0.5 --duration_ns 1500000 --seed 1 --out $o
done
