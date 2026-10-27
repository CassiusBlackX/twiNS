#!/bin/bash
# run_simai.sh NAME TOPO WORKLOAD THREADS [ENV=VAL ...]: one SimAI run in its own container,
# outputs in ~/netsim/aiwork/runs/NAME (fct.txt, csvs, out.txt, wall).
# V=debug (stock build, MTP) | mtp (optimized, MTP) | seq (optimized, sequential)
name=$1 topo=$2 wl=$3 thr=$4; shift 4
V=${V:-debug}
if [ $V = debug ]; then SD=/home/cassius/netsim/simai; BIN=/simai/bin/SimAI_simulator
else SD=/home/cassius/netsim/simai-$V; BIN=/simai/astra-sim-alibabacloud/extern/network_backend/ns3-interface/simulation/build/scratch/ns3.36.1-AstraSimNetwork-optimized; fi
R=/home/cassius/netsim/aiwork/runs/$name; rm -rf $R; mkdir -p $R/etc/simulation $R/etc/results
SIMAI_DIR=$SD ETC_DIR=$R/etc /home/cassius/netsim/podman/simai.sh "cd /work/runs/$name && export AS_SEND_LAT=3 $*; s=\$(date +%s%N); $BIN -t $thr -w /work/wl/$wl -n /work/topo/$topo -c /simai/astra-sim-alibabacloud/inputs/config/SimAI.conf > out.txt 2> err.txt; echo rc=\$? ns=\$((\$(date +%s%N)-s)) > wall; cp /etc/astra-sim/simulation/llama_hpn7_fct.txt fct.txt"
