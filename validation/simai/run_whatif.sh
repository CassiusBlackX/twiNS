#!/bin/bash
# run_whatif.sh MEM TMO CONFIG ...: the what-if matrix's traced SimAI runs (simai-trace64), all at
# once, each capped at MEM and TMO seconds; outputs in ~/netsim/aiwork/runs/w_LOAD_CONFIG. Start it
# with nohup. Then, per run, trace2dag.py gives w_*/run.{dag,ref}; the trace stays on a87.
cd ~/netsim/aiwork
mem=$1 tmo=$2; shift 2
LOADS=${LOADS:-mar:microAR_tp128.txt:128 moe:moe_16.txt:16 ar64:paper/c_allreduce_64m_128.txt:128 llp:paper/llama_65b_probe_128.txt:128 a2a:paper/c_alltoall_64m_128.txt:128}
topo() { # CONFIG GPUS
    case $1 in
    hpn) echo AlibabaHPN_$2g_8gps_DualToR_SinglePlane_200Gbps_H100 ;;
    dcn*) echo DCN+SingleToR_$2g_8gps_200Gbps_A100 ;;
    g200) echo Spectrum-X_$2g_8gps_200Gbps_H100 ;;
    *) echo Spectrum-X_$2g_8gps_400Gbps_H100 ;;
    esac
}
for c in "$@"; do
    for l in $LOADS; do
        IFS=: read load wl g <<< "$l"
        name=w_${load}_$c
        conf=
        [ -f conf/$c.conf ] && conf=/work/conf/$c.conf
        ( V=trace64 MEM=$mem TMO=$tmo CONF=$conf ./run_simai.sh $name $(topo $c $g) $wl 1 TWINS_TRACE=/work/runs/$name/trace.txt
          echo "$(date +%T) $name $(cat runs/$name/wall) $(grep -c . runs/$name/fct.txt 2>/dev/null) flows"
          python3 trace2dag.py runs/$name/trace.txt runs/$name/fct.txt runs/$name/run.dag runs/$name/run.ref ) &
    done
done
wait
echo "$(date +%T) ALLDONE"
