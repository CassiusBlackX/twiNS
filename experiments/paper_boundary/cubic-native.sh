#!/usr/bin/env bash
set -euo pipefail
root=$(pwd -P)
ref=/home/cassius/netsim/twins-tcp-followup-20261126/twins-paper-tcp-options/build/reference-v4/reference
mkdir cubic-native
cd cubic-native
sha256sum "$ref" "$root/k8_a2a30.csv" > inputs.sha256
TRACE_ALLQ=10000000000 timeout 240 "$ref" --k=8 "--flows=$root/k8_a2a30.csv" --tcp=ns3::TcpCubic \
 --queue=200 --ecnK=30 --flowEcmp=1 --hostq=red --threads=0 --fin=0 --stop=0.01 --out=flows.csv >run.log 2>&1
printf '0\n' >rc
