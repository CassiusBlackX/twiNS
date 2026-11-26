#!/usr/bin/env bash
set -euo pipefail
root=$(pwd -P)
out=$root/results
old=/home/cassius/Videos/twins-isolated-20261118/out
mkdir "$out"
trap 'rc=$?; printf "%s\n" "$rc" > "$out/exit-status"' EXIT
test -s "$old/export/all-finished.json"
exec 9>"$old/exclusive-campaign.lock"
flock 9
while [[ -n $(nvidia-smi --query-compute-apps=pid --format=csv,noheader) ]]; do sleep 10; done
mkdir -p out
ln -s /home/cassius/Videos/twins-paper-followup-20261123/candidate/out/astra out/astra
cmake -S . -B build -G Ninja -DTWINS_GPU_BACKEND=CUDA -DCMAKE_CUDA_ARCHITECTURES=120 \
    -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc '-DTWINS_EXTRA_DEFS=RDMA_LIFETIME_PROBE;RDMA_TIMER_AUDIT;RDMA_PFC_AUDIT' >"$out/build.log" 2>&1
cmake --build build --target twins-rdma twins-ai twins-island -j6 >>"$out/build.log" 2>&1
sha256sum build/twins-{rdma,ai,island} >"$out/binaries.sha256"
bash experiments/paper_full_review/late_probe.sh build/twins-rdma ../rdma-candidate/inputs "$out/late"
for delay in 2000 5000 10000; do
    target="$out/timer-$delay"
    mkdir "$target"
    input="$root/../rdma-candidate/inputs"
    ./build/twins-rdma "$input/hpcc.cfg" "--topo=$input/fattree8.txt" "--flows=$input/k8_a2a30.csv" \
      "--delay=$delay" "--out=$target/output.fct" CC_MODE=1 --fp-bases=1000003,1000033 >"$target/run.log" 2>&1
done
pfc=/home/cassius/Videos/twins-paper-strict-20261123/pfc_gpu_boundary/20261123T142123Z/results/pfc
for pool in 64 06; do
    source="$pfc/pool-$pool/input"
    for mode in checked serial cap1; do
        if [[ "$mode" == cap1 && "$pool" == 64 ]]; then continue; fi
        target="$out/pfc/$pool/$mode"
        mkdir -p "$target"
        more=()
        if [[ "$mode" == serial ]]; then more+=(--serial); fi
        if [[ "$mode" == cap1 ]]; then more+=(--q3cap=1); fi
        (
            cd "$target"
            rc=0
            /usr/bin/time -v timeout 180s taskset -c 0 "$root/build/twins-ai" \
              "$source/config.log" "--topo=$source/topo.log" "--astra=$source/workload.log" --out=messages.fct --dag-out=actions.dag \
              --nvl-islands=0 --gwin=0 --fp-bases=1000003,1000033 "${more[@]}" >run.log 2>&1 || rc=$?
            printf '%s\n' "$rc" >rc
            if [[ "$mode" != cap1 ]]; then test "$rc" = 0; fi
        )
    done
done
