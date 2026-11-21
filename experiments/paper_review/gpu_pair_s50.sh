#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
root=$(pwd -P)
evidence=$root/out/review/gpu-pair-20261122
target=/home/cassius/Videos/twins-paper-pair-20261122
base=/home/cassius/Videos/twins-paper-followthrough-20261122/project
ssh_cmd=(ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=20 s50)

if [[ $1 == remote ]]; then
    mkdir -p "$2"
    sha256sum -c SHA256SUMS > "$2/input-check.txt"
    cp SHA256SUMS "$2/"
    cmake -S GeDES_code -B build-gedes -G Ninja -DGEDES_GPU_BACKEND=CUDA -DCMAKE_CUDA_ARCHITECTURES=120 \
        -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc -DCMAKE_CUDA_FLAGS=-DENABLE_DCTCP=0 \
        > "$2/gedes-build.log" 2>&1
    cmake --build build-gedes --target GeDES -j2 >> "$2/gedes-build.log" 2>&1
    python3 experiments/paper_review/gpu_pair.py run common-inputs "$2/measurements" \
        "$base/build/twins" build-gedes/GeDES /home/cassius/Videos/days/target/release/days
    exit
fi

test "$1" = schedule
follow=$root/out/review/follow-through-20261122
until [[ -s "$follow/last-return" ]]; do sleep 30; done
stamp=$(date -u +%Y%m%dT%H%M%SZ)
dst="$evidence/results/$stamp"
mkdir -p "$dst"
"${ssh_cmd[@]}" "mkdir -p '$target/project'"
rsync -a -e 'ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=10' "$evidence/staged/" "s50:$target/project/"
rc=0
"${ssh_cmd[@]}" "cd '$target/project' && flock /home/cassius/Videos/twins-isolated-20261118/out/exclusive-campaign.lock \
   timeout 2h python3 experiments/paper_review/observe_cuda.py 'runs/$stamp' bash experiments/paper_review/gpu_pair_s50.sh remote 'runs/$stamp'" \
   > "$dst/worker.log" 2>&1 || rc=$?
printf '%s\n' "$rc" > "$dst/remote.rc"
rsync -a --checksum -e 'ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=10' "s50:$target/project/runs/$stamp/" "$dst/"
"${ssh_cmd[@]}" "set -o pipefail; cd '$target/project/runs/$stamp' && find . -type f -print0 | sort -z | xargs -0 -r sha256sum" \
    > "$dst/returned-files.sha256"
test -s "$dst/returned-files.sha256"
(cd "$dst" && sha256sum --quiet -c returned-files.sha256)
exit "$rc"
