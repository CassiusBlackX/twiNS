#!/bin/bash
# parameter sweep with ns-3, one configuration per core (32 at a time):
# ECN/RED threshold K x queue limit x TCP x load on k=8 websearch all-to-all
cd "${NS3_WORK:-$HOME/netsim/cmp}"
B=${NS3_BIN:-../unison/build/scratch/ns3.46.1-twins-ref-optimized}
jobs=sweep/jobs.txt; : > $jobs
for w in k8_a2a30 k8_a2a50w; do for tcp in TcpDctcp TcpCubic TcpNewReno; do for q in 100 200 400; do for K in 5 10 20 30 40 60 80 120; do
  [ $K -lt $q ] || continue
  echo "$w $tcp $q $K" >> $jobs
done; done; done; done
s=$(date +%s.%N)
cat $jobs | xargs -P 32 -L 1 ./sweep_one.sh
e=$(date +%s.%N)
echo "$(wc -l < $jobs) configurations, wall $(python3 -c "print(round($e-$s,1))") s (32 parallel)"
echo ALL DONE
