#!/bin/bash
# R-1.5 for the HPCC fork: each job of JOBFILE ("name topo flows [KEY=VALUE ...]", files in $WORK) runs
# in the patched fork (hpcc_evlog_patch.py, tree in $HPCC_DIR) with NS3_EVLOG on a FIFO that the
# oracle (oracle LOG rdma) reads as it is written: nothing is stored. Results in $WORK/oracle/NAME.txt.
# usage: oracle_rdma.sh JOBFILE [parallel]   env: WORK (~/netsim/rdma), HPCC_DIR (~/netsim/hpcc-evlog), ORACLE
WORK=${WORK:-$HOME/netsim/rdma}
export HPCC_DIR=${HPCC_DIR:-$HOME/netsim/hpcc-evlog} ORACLE=${ORACLE:-$HOME/netsim/oracle_src/oracle} WORK
cd "$WORK"
mkdir -p oracle
one() {
    n=$1 t=$2 f=$3
    shift 3
    [ -s oracle/$n.txt ] && return
    fifo=oracle/$n.fifo
    rm -f $fifo
    mkfifo $fifo
    "$ORACLE" $fifo rdma > oracle/$n.txt 2>&1 &
    op=$!
    ./hpcc.sh "cd /work && NS3_EVLOG=/work/$fifo LD_LIBRARY_PATH=/hpcc/simulation/build /hpcc/simulation/build/scratch/rdma-ref hpcc.cfg --topo=$t --flows=$f --out=oracle/$n.fct $*" > oracle/$n.log 2>&1
    wait $op
    echo "rc $?" >> oracle/$n.txt
    rm -f $fifo
    cmp -s oracle/$n.fct $n.fct && echo "fct same as the stored reference" >> oracle/$n.txt || echo "fct DIFFERS from the stored reference" >> oracle/$n.txt
    echo "$(date +%T) $n: $(grep -m1 '^all pairs' oracle/$n.txt | cut -c1-160)"
}
export -f one
grep -v '^#' "$1" | grep . | xargs -P "${2:-8}" -L 1 bash -c 'one "$@"' _
echo ALL DONE
