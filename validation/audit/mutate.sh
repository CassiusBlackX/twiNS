#!/bin/bash
# Mutation study (TODOS P0-3): each mutation is a plausible misreading of ns-3, selected at run time
# in a -DTWINS_MUTATION build (build-mut) with TWINS_MUT=n; a mutation is caught if any run of the
# test set differs from ns-3 in any flow's establishment or completion time. Mutation 0: none.
#  1 fast retransmit after 4 duplicate ACKs, not 3        14 DCTCP receiver: no "previous ACK" on a CE change
#  2 delayed ACK after every segment, not every second     15 no ACK for a segment entirely below rcv_nxt
#  3 delayed-ACK timeout 20 us, not 10 us                  16 listener does not keep CE sequence numbers
#  4 minimum RTO 1.2 ms, not 1 ms                          17 SYN timeout 3 s (older ns-3 default), not 10 ms
#  5 no exponential RTO backoff                            18 pure ACK frames without the PPP header (40 B)
#  6 DCTCP g = 1/8, not 1/16                               19 data frames without the PPP header
#  7 RED marks when the queue exceeds K, not reaches it    20 timer vs frame arrival at one instant: reversed
#  8 queue limit one packet larger                         21 two timers at one instant: reversed
#  9 SRTT gain 1/4, not 1/8                                22 spraying draws in port order, not event order
# 10 RTTVAR gain 1/8, not 1/4                              23 NewReno partial ACK: no retransmission
# 11 clock granularity 1 ns, not 1 us (RTO floor)          24 first data segment not ACKed at once
# 12 Cubic beta 0.8, not 0.7                               25 sender: every CE-marked ACK restarts the ECE state
# 13 Cubic C 0.5, not 0.4                                  26 source ports from 49152, not 49153 (ECMP hashes)
# usage: validation/audit/mutate.sh [first last]   env: BIN (build-mut)
cd "$(dirname "$0")/../.."
B=${BIN:-build-mut}
OUT=out/mut; mkdir -p $OUT
T=$OUT/tests.txt
{
  for w in k8_paper k8_a2a30 k8_incast16 k8_a2a50s; do for t in dctcp newreno cubic; do echo "${w}_$t data/ns3/ref/${w}_Tcp$(case $t in dctcp) echo Dctcp;; newreno) echo NewReno;; cubic) echo Cubic;; esac).csv --k=8 --flows=data/flows/$w.csv --tcp=$t"; done; done
  for t in dctcp newreno cubic; do echo "k8_a2a30_${t}_spray data/ns3/ref/k8_a2a30_Tcp$(case $t in dctcp) echo Dctcp;; newreno) echo NewReno;; cubic) echo Cubic;; esac)_spray.csv --k=8 --flows=data/flows/k8_a2a30.csv --tcp=$t --spray=1"; done
  for t in dctcp newreno; do echo "k8_r30_${t}_fin data/ns3/ref/k8_r30_Tcp$(case $t in dctcp) echo Dctcp;; newreno) echo NewReno;; cubic) echo Cubic;; esac)_fin.csv --k=8 --flows=data/flows/k8_r30.csv --tcp=$t --fin=1"; done
  echo "k8_r30_dctcp_sprayfin data/ns3/ref/k8_r30_TcpDctcp_sprayfin.csv --k=8 --flows=data/flows/k8_r30.csv --tcp=dctcp --spray=1 --fin=1"
  echo "jf32_a2a30_dctcp data/ns3/ref/jf32_a2a30_TcpDctcp.csv $(sed -n 's/^# twins //p' data/flows/jf32_a2a30.csv.topo) --flows=data/flows/jf32_a2a30.csv --tcp=dctcp"
  echo "ft8f_a2a30_dctcp data/ns3/ref/ft8f_a2a30_TcpDctcp.csv $(sed -n 's/^# twins //p' data/flows/ft8f_a2a30.csv.topo) --flows=data/flows/ft8f_a2a30.csv --tcp=dctcp"
  for g in g188 g000 g003 g010 g025 g050 g075 g100 g150 g199; do echo "$g data/ns3/grid/$g.csv $(grep "^$g " validation/grid/jobs_twins.txt | cut -d' ' -f2-)"; done
} > $T
for m in $(seq ${1:-0} ${2:-26}); do
  caught=0 runs=0 flows_diff=0 flagged=0 list=""
  while read -r name ref args; do
    o=$OUT/m$m.$name.csv
    r=$(TWINS_MUT=$m timeout 300 ./$B/twins $args --out=$o 2>&1)
    if echo "$r" | grep -q "were undecided"; then r=$(TWINS_MUT=$m timeout 300 ./$B/twins128 $args --out=$o 2>&1); fi
    echo "$r" | grep -q "were undecided\|a capacity overflowed" && flagged=$((flagged + 1))
    d=$(paste -d, <(tail -n +2 $ref | cut -d, -f5,6) <(tail -n +2 $o 2>/dev/null | cut -d, -f5,6) | awk -F, '{n++; if (!($1 == $3 && $2 == $4)) x++} END {print x + 0}')
    runs=$((runs + 1))
    if [ "$d" != 0 ]; then caught=$((caught + 1)); flows_diff=$((flows_diff + d)); list="$list $name"; fi
    rm -f $o
  done < $T
  echo "mutation $m: $caught of $runs runs differ ($flows_diff flows); runs with flags $flagged;$list"
done
