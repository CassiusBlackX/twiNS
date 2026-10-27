#!/bin/bash
# Run simai-ref jobs in the container, in parallel: each line of JOBFILE is "name topo flows
# [KEY=VALUE ...]" (files in $WORK; a flows file ending in .dag runs simai-dag-ref); writes name.fct, name.sent, name.tx (the transmission trace)
# and name.log (with the wall time) to $WORK. The config is $WORK/simai.cfg (SimAI.conf).
#
# usage: run_ref.sh JOBFILE [parallel]
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=${WORK:-$HOME/netsim/aiwork/ref}
SIMAI_DIR=${SIMAI_DIR:-$HOME/netsim/simai-ref}
JOBS=$(realpath "$1")
cd "$WORK"
export HERE WORK SIMAI_DIR
grep -v "^#" "$JOBS" | grep -v "^$" | xargs -P "${2:-16}" -L 1 bash -c 'n=$0; t=$1; f=$2; shift 2; b=simai-ref; i=--flows; case $f in *.dag) b=simai-dag-ref; i=--dag;; esac; s=$(date +%s.%N); WORK=$WORK SIMAI_DIR=$SIMAI_DIR "$HERE/simai.sh" "cd /work && /simai/astra-sim-alibabacloud/extern/network_backend/ns3-interface/simulation/build/scratch/ns3.36.1-$b-optimized simai.cfg --topo=$t $i=$f --out=$n.fct --sent=$n.sent --txtrace=$n.tx $*" > $n.log 2>&1; echo "wall $(echo "$(date +%s.%N) - $s" | bc) s" >> $n.log'
