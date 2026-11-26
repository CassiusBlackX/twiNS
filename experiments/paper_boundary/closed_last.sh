#!/usr/bin/env bash
set -euo pipefail
bin=$(realpath "$1")
config=$(realpath "$2")
topo=$(realpath "$3")
base=$(realpath "$4")
out=$(realpath -m "$5")
count=$(sed -n 's/.*callbacks, \([0-9][0-9]*\) syncs,.*/\1/p' "$base/cold/run.log")
mkdir "$out"
cd "$out"
rc=0
env "TWINS_STOP_SYNCS=$count" timeout 90 "$bin" "$config" "--topo=$topo" "--astra=$base/short.wl" \
  --out=messages.fct --dag-out=actions.dag --nvl-islands=0 --gwin=0 --fp-bases=1000003,1000033 >run.log 2>&1 || rc=$?
printf '%s\n' "$rc" >rc
