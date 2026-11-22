#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
remote=$(ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8 s50 \
    'ls -dt ~/Videos/twins-paper-followup-20261123/k32_cpu/20* | head -1')
ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=20 s50 \
    "python3 - '$remote/results'" < experiments/paper_followup/resource_samples.py \
    > out/followup-20261123/provenance/shared-resource-samples.log
