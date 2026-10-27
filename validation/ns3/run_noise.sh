#!/bin/bash
# extra noise-floor runs: start times jittered by 0-10 ns with seeds 1..4
cd "${NS3_WORK:-$HOME/netsim/cmp}"
B=${NS3_BIN:-../unison/build/scratch/ns3.46.1-twins-ref-optimized}
mkdir -p ref
for s in 1 2 3 4; do
for w in k8_paper k8_fullsync k8_fullexp k8_a2a30 k8_incast16 k8_a2a50s; do
  [ -f flows/${w}_jit$s.csv ] || python3 -c "
import random
r=random.Random(1000+$s); L=open(\"flows/$w.csv\").read().split(\"\n\"); o=[L[0]]
for l in L[1:]:
    if not l: continue
    f=l.split(\",\"); f[-1]=str(int(f[-1])+r.randint(0,10)); o.append(\",\".join(f))
open(\"flows/${w}_jit$s.csv\",\"w\").write(\"\n\".join(o)+\"\n\")"
  for tcp in TcpDctcp TcpNewReno; do
    o=ref/${w}_${tcp}_jit$s.csv
    [ -f $o ] || { $B --k=8 --flows=flows/${w}_jit$s.csv --tcp=ns3::$tcp --stop=0.3 --hostq=red --flowEcmp=1 --out=$o > $o.log 2>&1; echo "$(date +%T) $o"; } &
  done
done
done
wait
echo ALL DONE
