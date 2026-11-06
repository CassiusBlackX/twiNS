#!/bin/bash
# run_ref.sh CASES_DIR NAME... : run single-threaded ns-3-ub (on $UB_HOST, default s50i) for each
# case CASES_DIR/NAME and store normalized traces in out/ub/ref/NAME.{port,task,log}.
# Reference runs go to the 9950X by default (a87 is the SimAI session's machine).
set -e
HOST=${UB_HOST:-s50i}
RW=${UB_REMOTE:-Videos/netsim/ubwork}
BIN=${UB_BIN:-Videos/netsim/ns-3-ub/build/scratch/ns3.44-ub-quick-example}
J=${UB_JOBS:-8}
MEMKB=${UB_MEMKB:-16000000}
cd "$(dirname "$0")/../.."
CD=$1; shift
mkdir -p out/ub/ref
ssh $HOST "mkdir -p ~/$RW/cases"
rsync -aq validation/ub/tools/norm.py $HOST:~/$RW/norm.py
for n in "$@"; do rsync -aq --delete "$CD/$n/" $HOST:~/$RW/cases/$n/; done
ssh $HOST "cd ~/$RW && printf '%s\n' $* | xargs -P $J -I{} bash -c 'ulimit -v $MEMKB; rm -rf cases/{}/runlog; /usr/bin/time -f \"wall %e s, maxrss %M KB\" ~/$BIN --case-path=\$PWD/cases/{} > cases/{}.log 2>&1; echo rc=\$? >> cases/{}.log; python3 norm.py ref cases/{}/runlog cases/{}.n'"
for n in "$@"; do
    rsync -aq $HOST:~/$RW/cases/$n.n.port out/ub/ref/$n.port
    rsync -aq $HOST:~/$RW/cases/$n.n.task out/ub/ref/$n.task
    rsync -aq $HOST:~/$RW/cases/$n.log out/ub/ref/$n.log
    echo "$n: $(grep -E 'run    :' out/ub/ref/$n.log | sed 's/.*run *: //') $(grep -E '^wall' out/ub/ref/$n.log) $(tail -1 out/ub/ref/$n.log)"
done
