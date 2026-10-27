#!/bin/bash
cd "${NS3_WORK:-$HOME/netsim/cmp}"
B=${NS3_BIN:-../unison/build/scratch/ns3.46.1-twins-ref-optimized}
for v in jit jit1 jit2 jit3 jit4; do for w in k8_paper k8_fullsync k8_fullexp k8_a2a30 k8_incast16 k8_a2a50s; do
  o=ref/${w}_TcpCubic_$v.csv
  [ -f $o ] || $B --k=8 --flows=flows/${w}_$v.csv --tcp=ns3::TcpCubic --stop=0.3 --hostq=red --flowEcmp=1 --out=$o > $o.log 2>&1 &
done; done
wait
echo ALL DONE
