#!/bin/bash
cd ~/netsim/aiwork
run() { V=trace timeout ${TMO:-7200} ./run_simai.sh $1 $2 $3 1 TWINS_TRACE=/work/runs/$1/trace.txt; echo "$1 $(cat runs/$1/wall) $(grep -c . runs/$1/fct.txt) flows"; }
for w in llama gpt13 moe; do
  run a_${w}_sx16 Spectrum-X_16g_8gps_400Gbps_H100 ${w}_16.txt &
  run a_${w}_hpn16 AlibabaHPN_16g_8gps_DualToR_SinglePlane_200Gbps_H100 ${w}_16.txt &
  run a_${w}_dcn16 DCN+SingleToR_16g_8gps_200Gbps_A100 ${w}_16.txt &
done
wait
echo AICBDONE
