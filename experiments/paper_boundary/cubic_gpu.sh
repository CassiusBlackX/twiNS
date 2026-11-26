#!/usr/bin/env bash
set -euo pipefail
bin=$(realpath "$1")
flow=$(realpath "$2")
out=$(realpath -m "$3")
mkdir "$out"
cd "$out"
sha256sum "$bin" "$flow" >inputs.sha256
timeout 160 "$bin" --k=8 "--flows=$flow" --tcp=cubic --queue=200 --ecnK=30 --tcp-sack=0 --tcp-timestamps=0 \
  --drain --max_slots=10000 --qtrace_until=10000000000 --qtrace=queue.txt --out=flows.csv >run.log 2>&1
printf '0\n' >rc
