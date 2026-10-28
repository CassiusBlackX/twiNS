#!/bin/bash
# traced SimAI end-to-end runs (simai-trace): runs/NAME/{fct.txt,trace.txt,wall,out.txt}
cd ~/netsim/aiwork
run() { V=trace ./run_simai.sh $1 $2 $3 1 TWINS_TRACE=/work/runs/$1/trace.txt; echo "$1 $(cat runs/$1/wall)"; }
run tr_sx8 Spectrum-X_8g_8gps_400Gbps_H100 microAR_tp8.txt &
run tr_sx16 Spectrum-X_16g_8gps_400Gbps_H100 microAR_tp16.txt &
run tr_hpn16 AlibabaHPN_16g_8gps_DualToR_SinglePlane_200Gbps_H100 microAR_tp16.txt &
run tr_dcn16 DCN+SingleToR_16g_8gps_200Gbps_A100 microAR_tp16.txt &
run tr_sx128 Spectrum-X_128g_8gps_400Gbps_H100 microAR_tp128.txt &
run tr_hpn128 AlibabaHPN_128g_8gps_DualToR_SinglePlane_200Gbps_H100 microAR_tp128.txt &
run tr_dcn128 DCN+SingleToR_128g_8gps_200Gbps_A100 microAR_tp128.txt &
wait
echo E2EDONE
