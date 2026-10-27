#!/bin/bash
# one sweep configuration: workload tcp queue K [k]
cd "${NS3_WORK:-$HOME/netsim/cmp}"
o=sweep/$1_$2_q$3_K$4.csv
[ -f $o ] || ../unison/build/scratch/ns3.46.1-twins-ref-optimized --k=${5:-8} --flows=flows/$1.csv --tcp=ns3::$2 --queue=$3 --ecnK=$4 --stop=0.3 --hostq=red --flowEcmp=1 --out=$o > $o.log 2>&1
