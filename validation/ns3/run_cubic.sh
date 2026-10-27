#!/bin/bash
# ns-3 references with TcpCubic (no ECN; RED drops at 30 packets)
cd "${NS3_WORK:-$HOME/netsim/cmp}"
B=${NS3_BIN:-../unison/build/scratch/ns3.46.1-twins-ref-optimized}
for w in ${WL:-k8_paper k8_fullsync k8_fullexp k8_a2a30 k8_incast16 k8_a2a50s}; do
  k=$(echo $w | sed "s/k\([0-9]*\)_.*/\1/")
  o=ref/${w}_TcpCubic.csv
  [ -f $o ] || { $B --k=$k --flows=flows/$w.csv --tcp=ns3::TcpCubic --stop=0.3 --hostq=red --flowEcmp=1 --out=$o > $o.log 2>&1; echo "$(date +%T) $o $(tail -1 $o.log | cut -c1-120)"; } &
done
wait
echo ALL DONE
