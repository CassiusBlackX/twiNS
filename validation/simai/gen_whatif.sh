#!/bin/bash
# gen_whatif.sh: inputs of the what-if matrix (docs/ai/whatif.md), in ~/netsim/aiwork on a87: the
# configs (conf/) and the Spectrum-X topologies with 200G NICs (topo/). Configurations:
#   sx  Spectrum-X, DCQCN, 32 MB (the recording, "A"); hpn, dcn: other topologies; cc3: HPCC;
#   bN: BUFFER_SIZE N MB; g200: NICs at 200G
A=~/netsim/simai-trace64/astra-sim-alibabacloud/inputs
W=~/netsim/aiwork
mkdir -p $W/conf
sed 's/^CC_MODE .*/CC_MODE 3/' $A/config/SimAI.conf > $W/conf/cc3.conf
for b in 1 2 4 8; do
    sed "s/^BUFFER_SIZE .*/BUFFER_SIZE $b/" $A/config/SimAI.conf > $W/conf/b$b.conf
done
cd $W/topo
for g in 16 128; do
    python3 $A/topo/gen_Topo_Template.py -topo Spectrum-X -g $g -gps 8 -gt H100 -bw 200Gbps -nvbw 2880Gbps -er 0
done
ls -la $W/conf $W/topo | grep "conf\|200Gbps_H100"
