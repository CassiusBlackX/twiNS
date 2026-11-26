#!/usr/bin/env bash
set -euo pipefail
mode=$1
bin=$(realpath "$2")
flows=$(realpath "$3")
out=$(realpath -m "$4")
mkdir "$out"
cd "$out"
sha256sum "$bin" "$flows" >inputs.sha256
if [[ "$mode" == native ]]; then
    TRACE_ALLQ=100000000000 timeout 300 "$bin" --k=8 "--flows=$flows" --tcp=ns3::TcpCubic --queue=200 --ecnK=30 \
       --flowEcmp=1 --hostq=red --threads=0 --fin=0 --stop=0.1 --out=flows.csv >run.log 2>&1
    sed -n 's/^Q //p' run.log | LC_ALL=C sort -s -k1,1n -k2,2n -k3,3n >queue.txt
else
    timeout 140 "$bin" --k=8 "--flows=$flows" --tcp=cubic --queue=200 --ecnK=30 --tcp-sack=0 --tcp-timestamps=0 --drain \
       --max_slots=100000 --qtrace_until=100000000000 --qtrace=queue.txt --out=flows.csv >run.log 2>&1
fi
sha256sum queue.txt flows.csv >outputs.sha256
printf '0\n' >rc
