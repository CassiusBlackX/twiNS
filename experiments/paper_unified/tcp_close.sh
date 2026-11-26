#!/usr/bin/env bash
set -euo pipefail
mode=$1
bin=$(realpath "$2")
input=$(realpath "$3")
out=$(realpath -m "$4")
mkdir -p "$out"
shift 4
for scenario in reuse incast; do
  cc=newreno
  native_cc=ns3::TcpNewReno
  q=200
  flows="$input/k8_r30.csv"
  if [[ $scenario == incast ]]; then flows="$input/k8_incast16.csv";q=3;fi
  mkdir "$out/$scenario"
  (
   cd "$out/$scenario"
   rc=0
   common=("--flows=$flows" --k=8 "--queue=$q" --fin=1 --tcp-sack=1 --tcp-timestamps=1 --out=flows.csv)
   if [[ "$mode" == native ]]; then
      TRACE_ALLQ=300000000000 timeout 125 "$bin" "--tcp=$native_cc" --threads=0 --flowEcmp=1 --hostq=red --stop=0.3 \
          "${common[@]}" >run.log 2>&1 || rc=$?
      sed -n 's/^Q //p' run.log | LC_ALL=C sort -s -k1,1n -k2,2n -k3,3n >queue.txt
   else
      timeout 180 "$bin" "--tcp=$cc" --drain --max_slots=300000 --qtrace=queue.txt --qtrace_until=299999999999 \
          "${common[@]}" >run.log 2>&1 || rc=$?
   fi
   printf '%s\n' "$rc" >rc
  )
done
