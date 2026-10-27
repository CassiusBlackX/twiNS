#!/bin/bash
# sequential ns-3 references at k=16 (exactness needs sequential event order)
cd "${NS3_WORK:-$HOME/netsim/cmp}"
B=${NS3_BIN:-../unison/build/scratch/ns3.46.1-twins-ref-optimized}
for w in k16_paper k16_a2a30 k16_incast16 k16_a2a50s; do
  for tcp in TcpDctcp TcpNewReno; do
    o=ref/${w}_${tcp}.csv
    [ -f $o ] || { /usr/bin/time -f "%e s %M KB" $B --k=16 --flows=flows/$w.csv --tcp=ns3::$tcp --stop=0.3 --hostq=red --flowEcmp=1 --out=$o > $o.log 2>&1; echo "$(date +%T) $o $(tail -2 $o.log | tr "\n" " ")"; } &
  done
done
wait
echo ALL DONE
