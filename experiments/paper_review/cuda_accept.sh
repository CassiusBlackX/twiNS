#!/usr/bin/env bash
set -euo pipefail

phase=$1
prior_host=/home/cassius/Videos/twins-isolated-20261118/out
review_host=/home/cassius/Videos/twins-paper-review-20261120
astro_host=/home/cassius/Videos/twins-extensions-20261119/out/astra
root=$(cd "$(dirname "$0")/../.." && pwd -P)
cd "$root"
review=$root/out/review
queue=$review/cuda-queue
prior=$root/../twins-paper-scale/out/campaign
ssh_cmd=(ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=20 -o ServerAliveCountMax=3 s50)

if [[ $phase == stage ]]; then
    stage=$(mktemp -d "$review/.cuda-stage.XXXXXX")
    git archive HEAD CMakeLists.txt src \
        experiments/paper_scale/gpu.py experiments/paper_scale/irn_diag.py \
        experiments/paper_review/{cuda_accept,irn_anchors}.sh \
        experiments/paper_review/{ai128,prefix}.py | tar -x -C "$stage"
    mkdir -p "$stage/inputs" "$stage/inputs/matrix/101" "$stage/inputs/matrix/103"
    for spec in "101/plain:$prior/inputs" "101/ecn:$prior/dcqcn/inputs" \
                "103/plain:$review/irn-holdout/plain/inputs" "103/ecn:$review/irn-holdout/ecn/inputs"; do
        rsync -a --include='*/' --include=manifest.json --include=uniform.graph \
            --include=flows.csv --include=config --exclude='*' "${spec#*:}/" "$stage/inputs/matrix/${spec%%:*}/"
    done
    rsync -a --include='*/' --include=graph --include=flows.csv --include=config --exclude='*' \
        "$prior/anchors/irn/" "$stage/inputs/anchors/"
    rsync -a --include='*/' --include=fabric.txt --include=simai.topo --include=config --include=workload.txt \
        --include=ref.flows --exclude='*' "$prior/ai-cap/" "$stage/inputs/ai-cap/"
    cp -r "$review/ai128-v3" "$stage/inputs/ai128"
    git rev-parse HEAD > "$stage/revision"
    (cd "$stage" && find inputs -type f -print0 | sort -z | xargs -0 sha256sum > inputs.sha256)
    mv -T "$stage" "$queue"
    exit
fi

if [[ $phase == remote ]]; then
    # Each remote invocation has a distinct result directory. The caller holds
    # the shared benchmark lock until the command ends.
    out=$2
    mkdir -p "$out" build
    sha256sum -c inputs.sha256 > "$out/input-check.txt"
    cp revision inputs.sha256 "$out/"
    nvidia-smi -q > "$out/nvidia-smi.txt"
    /usr/local/cuda/bin/nvcc --version > "$out/nvcc.txt"
    mkdir -p out
    ln -sfnT "$astro_host" out/astra
    cmake -S . -B build -G Ninja -DTWINS_GPU_BACKEND=CUDA -DCMAKE_CUDA_ARCHITECTURES=120 \
        -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc > "$out/build.log" 2>&1
    cmake --build build --target twins-rdma twins-ai -j2 >> "$out/build.log" 2>&1
    sha256sum build/twins-rdma build/twins-ai > "$out/executables.sha256"
    for seed in 101 103; do
        for kind in plain ecn; do
            python3 experiments/paper_scale/gpu.py "inputs/matrix/$seed/$kind" "$out/matrix/$seed/$kind" build
        done
    done
    bash experiments/paper_review/irn_anchors.sh build/twins-rdma inputs/anchors "$out/anchors"
    mkdir -p "$out/wire-layout"
    ln -s "$root/inputs/matrix/101/ecn" "$out/wire-layout/inputs"
    for mode in serial parallel; do
        flag=--serial
        [[ $mode != parallel ]] || flag=--irn-parallel
        python3 experiments/paper_scale/irn_diag.py "$out/wire-layout" build/twins-rdma gpu \
            --out "$out/wire/$mode" --gpu-option="$flag"
    done
    for name in small large; do
        python3 experiments/paper_review/prefix.py inputs "$out/ai-$name" build/twins-ai gpu "ai-$name" || true
    done
    cp -r inputs/ai128 "$out/ai128"
    python3 experiments/paper_review/ai128.py "$out/ai128" gpu --binary=build/twins-ai --wire-until=100000
    exit
fi

[[ $phase == schedule ]]
test -s "$queue/revision"
log=$review/cuda-queue.log
receipt=$prior/s50/finished.json
while [[ ! -s $receipt ]]; do
    printf '%s waiting for the existing s50 successor queue\n' "$(date -Is)" >> "$log"
    sleep 300
done
# Do not compete with a job that took the GPU after the predecessor completed.
until [[ $("${ssh_cmd[@]}" 'nvidia-smi --query-compute-apps=pid --format=csv,noheader' || printf unknown) == '' ]]; do
    sleep 60
done
stamp=$(date -u +%Y%m%dT%H%M%SZ)
sent=$review/cuda-results/$stamp
mkdir -p "$sent"
"${ssh_cmd[@]}" "mkdir -p '$review_host/project'"
RSYNC_RSH='ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=20' \
    rsync -a "$queue/" "s50:$review_host/project/"
set +e
"${ssh_cmd[@]}" "cd '$review_host/project' && flock '$prior_host/exclusive-campaign.lock' timeout 4h \
    bash -ec 'mkdir -p results/$stamp; gpu_pids=\$(nvidia-smi --query-compute-apps=pid --format=csv,noheader); \
    test -z \"\$gpu_pids\" || exit 75; bash experiments/paper_review/cuda_accept.sh remote results/$stamp'" > "$sent/worker.log" 2>&1
rc=$?
set -e
printf '%s\n' "$rc" > "$sent/remote.rc"
RSYNC_RSH='ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=20' \
    rsync -a --checksum --exclude=wire-layout "s50:$review_host/project/results/$stamp/" "$sent/"
"${ssh_cmd[@]}" "cd '$review_host/project/results/$stamp' && find . -type f -print0 | sort -z | xargs -0 -r sha256sum" \
    > "$sent/returned-files.sha256"
if [[ -s $sent/returned-files.sha256 ]]; then
    (cd "$sent" && sha256sum --quiet -c returned-files.sha256)
    # Keep the canonical raw results only on this machine after verification.
    "${ssh_cmd[@]}" "cd '$review_host/project/results' && test \"\$(pwd -P)\" = '$review_host/project/results' && \
        test -d '$stamp' && test ! -L '$stamp' && rm -rf -- '$stamp'"
fi
python3 experiments/paper_review/cuda_report.py "$prior" "$review" "$sent" > "$sent/acceptance.json"
exit "$rc"
