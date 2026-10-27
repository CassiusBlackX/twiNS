#!/bin/bash
# k=16 sweep with ns-3, one configuration per core (32 at a time)
cd "${NS3_WORK:-$HOME/netsim/cmp}"
jobs=sweep/jobs16.txt; : > $jobs
for w in k16_a2a30 k16_a2a50w; do for tcp in TcpDctcp TcpCubic TcpNewReno; do for q in 100 400; do for K in 10 20 40 80; do
  echo "$w $tcp $q $K 16" >> $jobs
done; done; done; done
s=$(date +%s.%N)
cat $jobs | xargs -P 32 -L 1 ./sweep_one.sh
e=$(date +%s.%N)
echo "$(wc -l < $jobs) configurations, wall $(python3 -c "print(round($e-$s,1))") s (32 parallel)"
echo ALL DONE
