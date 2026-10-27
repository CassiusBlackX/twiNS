#!/bin/bash
# ns-3 reference for TwiNS: per-flow ECMP (sorted routes), host RED, classic recovery,
# no limited transmit; plus a noise-floor run with start times jittered by 0-10 ns
cd "${NS3_WORK:-$HOME/netsim/cmp}"
B=${NS3_BIN:-../unison/build/scratch/ns3.46.1-twins-ref-optimized}
mkdir -p ref
run() { local o=ref/$1.csv; shift; [ -f $o ] && return; $B "$@" --out=$o > $o.log 2>&1; echo "$(date +%T) $(basename $o): $(tail -1 $o.log)"; }
for w in k8_paper k8_fullsync k8_fullexp k8_a2a30 k8_incast16 k8_a2a50s; do
  for tcp in TcpDctcp TcpNewReno; do
    run ${w}_${tcp} --k=8 --flows=flows/$w.csv --tcp=ns3::$tcp --stop=0.3 --hostq=red --flowEcmp=1 &
    [ -f flows/${w}_jit.csv ] || python3 -c "
import random,sys
r=random.Random(99); L=open(\"flows/$w.csv\").read().split(\"\n\"); o=[L[0]]
for l in L[1:]:
    if not l: continue
    f=l.split(\",\"); f[-1]=str(int(f[-1])+r.randint(0,10)); o.append(\",\".join(f))
open(\"flows/${w}_jit.csv\",\"w\").write(\"\n\".join(o)+\"\n\")"
    run ${w}_${tcp}_jit --k=8 --flows=flows/${w}_jit.csv --tcp=ns3::$tcp --stop=0.3 --hostq=red --flowEcmp=1 &
  done
done
wait
echo ALL DONE
