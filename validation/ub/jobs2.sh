#!/bin/bash
# jobs2: per-packet routing (spray): HASH64, ADAPTIVE and ROUND_ROBIN selectors
G="$HOME/.venvs/ub/bin/python validation/ub/gen_cases.py out/ub/cases"
SP="set:ns3::UbTransportChannel::RoutingType=PER_PACKET_SHORTEST_PATHS set:ns3::UbApp::RoutingType=PER_PACKET_SHORTEST_PATHS"
$G sp32_a2a  clos hosts=32 leaves=4 spines=4 traffic=alltoall size=8MB $SP
$G ad32_a2a  clos hosts=32 leaves=4 spines=4 traffic=alltoall size=8MB $SP set:ns3::UbRoutingProcess::MultipathSelector=ADAPTIVE
$G rr32_a2a  clos hosts=32 leaves=4 spines=4 traffic=alltoall size=8MB $SP set:ns3::UbRoutingProcess::MultipathSelector=ROUND_ROBIN
$G ad32_inc  clos hosts=32 leaves=4 spines=4 traffic=incast flows=256 dsts=8 size=r300000 seed=2 $SP set:ns3::UbRoutingProcess::MultipathSelector=ADAPTIVE
$G sp32_rnd  clos hosts=32 leaves=4 spines=4 traffic=rand flows=512 size=r200000 seed=3 $SP
$G ad32_os   clos hosts=32 leaves=4 spines=2 bw=200Gbps delay=100ns hbw=400Gbps hdelay=10ns traffic=rand flows=256 size=r500000 seed=4 $SP set:ns3::UbRoutingProcess::MultipathSelector=ADAPTIVE
$G ad128_a2a clos hosts=128 leaves=8 spines=8 traffic=alltoall size=4MB $SP set:ns3::UbRoutingProcess::MultipathSelector=ADAPTIVE
$G rr128_inc clos hosts=128 leaves=8 spines=8 traffic=incast flows=1024 dsts=16 size=r1000000 seed=5 $SP set:ns3::UbRoutingProcess::MultipathSelector=ROUND_ROBIN
