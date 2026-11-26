#!/usr/bin/env bash
set -euo pipefail
p=$(pwd -P)
base=/home/cassius/netsim/twins-boundary-review
python3 dense.py dense-native .
for n in 63 64 65; do
 (cd "$p/dense-native/$n"
  LD_LIBRARY_PATH="$base/conweave-stock/build" TWINS_IRN_LOW_NS=1000 TWINS_TXTRACE=wire.raw TWINS_TXTRACE_MAX_NS=2000099999 \
    timeout 75s "$base/irn-stock/reference" config > run.log 2>&1
  printf '0\n' >rc)
done
