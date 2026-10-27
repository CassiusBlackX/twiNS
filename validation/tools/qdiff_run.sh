#!/bin/bash
# Runs TwiNS and ns-3 on the same configuration with queue tracing and prints the first
# difference of the two enqueue histories.
# usage: validation/tools/qdiff_run.sh "<twins args>" "<twins-ref args>" [until_ps] [from_ps]
#   (flow paths: data/flows/X.csv for TwiNS, flows/X.csv on the ns-3 host; ssh host $NS3_HOST)
cd "$(dirname "$0")/../.."
W=${TMPDIR:-/tmp}/qdiff.$$
mkdir -p $W
T=${3:-1000000000}
F0=${4:-0}
./build/twins $1 --out=$W/o.csv --qtrace=$W/ours.txt --qtrace_from=$F0 --qtrace_until=$T | grep qtrace
ssh ${NS3_HOST:-a87} "cd \${NS3_WORK:-\$HOME/netsim/cmp} && TRACE_ALLQ_FROM=$F0 TRACE_ALLQ=$T \${NS3_BIN:-../unison/build/scratch/ns3.46.1-twins-ref-optimized} $2 --out=/tmp/qd.csv | grep '^Q ' > /tmp/qd.txt"
scp -q ${NS3_HOST:-a87}:/tmp/qd.txt $W/ns3.txt
python3 validation/tools/qdiff.py $W/ns3.txt $W/ours.txt
rm -rf $W
