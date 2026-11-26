#!/usr/bin/env bash
set -euo pipefail
root=$(pwd -P)
out=$root/rdma-results
old=/home/cassius/Videos/twins-isolated-20261118/out
paper=/home/cassius/Videos/twins-paper-strict-20261123
mkdir -p "$out"
test -s "$old/export/all-finished.json"
while [[ ! -f ../tcp-results/exit-status ]]; do sleep 10; done
exec 9>"$old/exclusive-campaign.lock"
flock 9
while [[ -n $(nvidia-smi --query-compute-apps=pid --format=csv,noheader) ]]; do sleep 10; done
trap 'rc=$?; printf "%s\n" "$rc" >"$out/exit-status"' EXIT
mkdir -p out
ln -s /home/cassius/Videos/twins-paper-followup-20261123/candidate/out/astra out/astra
cmake -S . -B build -G Ninja -DTWINS_GPU_BACKEND=CUDA -DCMAKE_CUDA_ARCHITECTURES=120 \
    -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc >"$out/build.log" 2>&1
cmake --build build --target twins-rdma twins-ai twins-island -j6 >>"$out/build.log" 2>&1
sha256sum build/twins-{rdma,ai,island} > "$out/binaries.sha256"
python3 experiments/paper_full_review/dcqcn.py gpu build/twins-rdma inputs "$out/dcqcn"
python3 experiments/paper_full_review/rdma_scale.py build/twins-rdma inputs/k40 \
   "$paper/rdma_scale_finish/20261125T162123Z/results/input" \
   "$paper/rdma_scale_compact_reference/20261125T160521Z/results/frozen-k32" "$out/scale" || true
pfc=$paper/pfc_gpu_boundary/20261123T142123Z/results
for pool in 06 64; do
    source="$pfc/pfc/pool-$pool/input"
    for mode in checked serial; do
        target="$out/pfc/$pool/$mode"
        mkdir -p "$target"
        more=()
        if [[ "$mode" == serial ]]; then more+=(--serial); fi
        (cd "$target" && /usr/bin/time -v timeout 180s taskset -c 0 "$pfc/build/diagnostic/compiled/twins-ai" \
          "$source/config.log" "--topo=$source/topo.log" "--astra=$source/workload.log" --out=messages.fct --dag-out=actions.dag \
          --nvl-islands=0 --gwin=0 --fp-bases=1000003,1000033 "${more[@]}" >run.log 2>&1)
    done
done
python3 experiments/paper_full_review/ai_profile.py build/twins-ai "$out/ai"
