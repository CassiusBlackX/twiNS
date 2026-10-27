#!/bin/bash
# Run rdma-ref jobs in the container, in parallel: each line of JOBFILE is "name topo flows
# [KEY=VALUE ...]" (files in $WORK); writes name.fct, name.tx (the transmission trace) and
# name.log (with the wall time) to $WORK.
#
# usage: run_jobs.sh JOBFILE [parallel]
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=${WORK:-$HOME/netsim/rdma}
cd "$WORK"
export HERE
grep -v "^#" "$1" | grep -v "^$" | xargs -P "${2:-16}" -L 1 bash -c 'n=$0; t=$1; f=$2; shift 2; /usr/bin/time -f "%e s %M KB" "$HERE/hpcc.sh" "cd /work && LD_LIBRARY_PATH=/hpcc/simulation/build /hpcc/simulation/build/scratch/rdma-ref hpcc.cfg --topo=$t --flows=$f --out=$n.fct --txtrace=$n.tx $*" > $n.log 2>&1'
