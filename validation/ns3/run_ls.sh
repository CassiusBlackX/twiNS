#!/bin/bash
# ns-3 references on a leaf-spine: 16 leaves x 16 hosts, 8 spines (2:1), all 100 Gbps
cd "${NS3_WORK:-$HOME/netsim/cmp}"
B=${NS3_BIN:-../unison/build/scratch/ns3.46.1-twins-ref-optimized}
for w in ls_paper ls_a2a30 ls_incast16 ls_a2a50s; do for tcp in TcpDctcp TcpNewReno TcpCubic; do
  o=ref/${w}_$tcp.csv
  [ -f $o ] || { $B --topo=leafspine --leaves=16 --spines=8 --hpl=16 --flows=flows/$w.csv --tcp=ns3::$tcp --stop=0.3 --hostq=red --flowEcmp=1 --out=$o > $o.log 2>&1; echo "$(date +%T) $o $(tail -1 $o.log | cut -c1-100)"; } &
done; done
wait
echo ALL DONE
