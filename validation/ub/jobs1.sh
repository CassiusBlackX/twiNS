#!/bin/bash
# jobs1: first validation matrix (base feature set). Generates the cases into out/ub/cases.
G="$HOME/.venvs/ub/bin/python validation/ub/gen_cases.py out/ub/cases"
$G a32_8m   clos hosts=32 leaves=4 spines=4 traffic=alltoall size=8MB
$G a32_32m  clos hosts=32 leaves=4 spines=4 traffic=alltoall size=32MB
$G inc32_1m clos hosts=32 leaves=4 spines=4 traffic=incast flows=128 dsts=4 size=1048576
$G inc32_r  clos hosts=32 leaves=4 spines=4 traffic=incast flows=256 dsts=8 size=r300000 seed=2
$G rnd32    clos hosts=32 leaves=4 spines=4 traffic=rand flows=512 size=r200000 seed=3
$G fwd32    clos hosts=32 leaves=4 spines=4 alloc=5ns fwd=50ns traffic=alltoall size=4MB
$G os32     clos hosts=32 leaves=4 spines=2 bw=200Gbps delay=100ns hbw=400Gbps hdelay=10ns traffic=rand flows=256 size=r500000 seed=4
$G ar32     clos hosts=32 leaves=4 spines=4 traffic=allreduce algo=ring size=16MB
$G a128_4m  clos hosts=128 leaves=8 spines=8 traffic=alltoall size=4MB
$G inc128   clos hosts=128 leaves=8 spines=8 traffic=incast flows=1024 dsts=16 size=r1000000 seed=5
