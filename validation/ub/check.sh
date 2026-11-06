#!/bin/bash
# check.sh CASES_DIR NAME... : run twins-ubseq ($UB_SIM, default build/twins-ubseq) on each case and
# compare per-port packet events and task start/complete times with out/ub/ref/NAME.*
cd "$(dirname "$0")/../.."
SIM=${UB_SIM:-build/twins-ubseq}
CD=$1; shift
mkdir -p out/ub/t
ok=0; bad=0
for n in "$@"; do
    if ! timeout ${UB_TIMEOUT:-600} $SIM $CD/$n --out=out/ub/t/$n $UB_ARGS 2> out/ub/t/$n.err; then
        echo "$n: FAILED $(tail -1 out/ub/t/$n.err)"; bad=$((bad+1)); continue
    fi
    python3 validation/ub/tools/norm.py twins out/ub/t/$n out/ub/t/$n.n
    if cmp -s out/ub/t/$n.n.port out/ub/ref/$n.port && cmp -s out/ub/t/$n.n.task out/ub/ref/$n.task; then
        echo "$n: identical ($(wc -l < out/ub/ref/$n.port) port events, $(grep -c ' C$\| C ' out/ub/ref/$n.task) task lines)"; ok=$((ok+1))
    else
        echo "$n: DIFFERS port:$(diff out/ub/t/$n.n.port out/ub/ref/$n.port | grep -c '^<') task:$(diff out/ub/t/$n.n.task out/ub/ref/$n.task | grep -c '^<')"; bad=$((bad+1))
    fi
done
echo "identical $ok, different $bad"
[ $bad = 0 ]
