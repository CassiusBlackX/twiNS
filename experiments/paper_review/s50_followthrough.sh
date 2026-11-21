#!/usr/bin/env bash
# Frozen, same-host checks after the existing CUDA correctness suite.
set -euo pipefail
cd "$(dirname "$0")/../.."
root=$(pwd -P)
review=$root/out/review
prior=$root/../twins-paper-scale/out/campaign
evidence=$review/follow-through-20261122
target=/home/cassius/Videos/twins-paper-followthrough-20261122
previous_host=/home/cassius/Videos/twins-paper-scale-20261119
review_host=/home/cassius/Videos/twins-paper-review-20261120
extension_host=/home/cassius/Videos/twins-extensions-20261119
ssh_cmd=(ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=20 s50)

if [[ ${1:-} == remote ]]; then
    out=$(realpath -m "$2")
    mkdir -p "$out"
    ulimit -c 0
    sha256sum -c staged-inputs.sha256 > "$out/input-check.txt"
    cp revision gates.json staged-inputs.sha256 "$out/"
    nvidia-smi -q > "$out/gpu-before.txt"
    /usr/local/cuda/bin/nvcc --version > "$out/nvcc.txt"
    lscpu > "$out/cpu.txt"
    cmake -S . -B build -G Ninja -DTWINS_GPU_BACKEND=CUDA -DCMAKE_CUDA_ARCHITECTURES=120 \
        -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc -DCMAKE_EXPORT_COMPILE_COMMANDS=ON > "$out/build.log" 2>&1
    cmake --build build --target twins -j2 >> "$out/build.log" 2>&1
    cp build/compile_commands.json "$out/"
    tcp=experiments/paper_review/tcp_wide_matrix.py
    rc=0
    python3 "$tcp" data-input/tcp data-input/ns3-101 data-input/ns3-102 build "$out/tcp-validation" --core 0 || rc=$?
    printf '%s\n' "$rc" > "$out/tcp-validation.rc"
    if (( rc == 0 )); then
        python3 "$tcp" data-input/tcp data-input/ns3-101 data-input/ns3-102 build "$out/tcp-timing" \
            --process-timing --core 0 --cpu-reference-bin "$extension_host/tcp-oracle/reference" \
            || printf '%s\n' "$?" > "$out/tcp-timing.rc"
    fi
    irn_ok=$(python3 -c 'import json; print(int(json.load(open("gates.json"))["irn"]))')
    ai_ok=$(python3 -c 'import json; print(int(json.load(open("gates.json"))["ai"]))')
    if (( irn_ok )); then
        sha256sum "$review_host/project/build/twins-rdma" > "$out/irn-executable.sha256"
        for mode in spread incast; do
            for size in 128 256 512 1024; do
                if ! python3 experiments/paper_scale/scale.py "$previous_host/inputs" \
                    "$out/irn-serial/$mode-$size" "$review_host/project/build" \
                    "$extension_host/tcp-oracle/reference" "$extension_host/irn-oracle" \
                    --irn-reference "$previous_host/live/run" --hosts "$size" --mode "$mode" --core 0 --timeout 600 \
                    --gpu-option=--serial; then
                    printf '%s %s\n' "$mode" "$size" >> "$out/irn-serial-failures.txt"
                fi
            done
            if ! python3 experiments/paper_scale/scale.py "$previous_host/inputs" \
                "$out/irn-parallel/$mode-1024" "$review_host/project/build" \
                "$extension_host/tcp-oracle/reference" "$extension_host/irn-oracle" \
                --irn-reference "$previous_host/live/run" --hosts 1024 --mode "$mode" --core 0 --timeout 600 \
                --gpu-option=--irn-parallel; then
                printf '%s\n' "$mode" >> "$out/irn-parallel-failures.txt"
            fi
        done
    fi
    if (( ai_ok )); then
        ai="$review_host/project/build/twins-ai"
        sha256sum "$ai" > "$out/ai-executable.sha256"
        for i in 0 1 2 3; do
            for workload in small large; do
                python3 experiments/paper_review/prefix.py data-input "$out/ai-$workload/trial-$i" \
                    "$ai" gpu "ai-$workload" --no-wire \
                    || printf '%s %s\n' "$workload" "$i" >> "$out/ai-failures.txt"
            done
            d="$out/ai128/trial-$i"
            mkdir -p "$d"
            cp -r data-input/ai128/. "$d/"
            python3 experiments/paper_review/ai128.py "$d" gpu --binary="$ai" \
                || printf '128 %s\n' "$i" >> "$out/ai-failures.txt"
        done
    fi
    exit
fi

mkdir -p "$evidence"
if [[ ${1:-} == stage ]]; then
    stage=$(mktemp -d "$evidence/.stage.XXXXXX")
    git archive HEAD CMakeLists.txt src validation/extensions \
        experiments/paper_review/s50_followthrough.sh experiments/paper_review/{tcp_wide_matrix,prefix,ai128,observe_cuda}.py \
        experiments/paper_scale/{scale,reference_hashes,ecn}.py | tar -x -C "$stage"
    mkdir -p "$stage/data-input"
    rsync -a --include='*/' --include=uniform.graph --include=heterogeneous.graph --include=flows.csv \
        --exclude='*' "$prior/inputs/" "$stage/data-input/tcp/"
    rsync -a --include='*/' --include=reference.fct --exclude='*' \
        "$prior/a87/cpu-scale/" "$stage/data-input/ns3-101/"
    rsync -a --include='*/' --include=reference.fct --exclude='*' \
        "$review/a87-final/tcp-seed102/cpu-scale/" "$stage/data-input/ns3-102/"
    rsync -a --include='*/' --include=fabric.txt --include=simai.topo --include=config \
        --include=ref.flows --include=workload.txt --exclude='*' "$prior/ai-cap/" "$stage/data-input/ai-cap/"
    mkdir -p "$stage/data-input/ai128"
    for name in fabric.txt simai.topo config ref.flows workload.txt simai.conf; do
        cp "$review/ai128-v3/$name" "$stage/data-input/ai128/"
    done
    git rev-parse HEAD > "$stage/revision"
    (cd "$stage" && find data-input -type f -print0 | sort -z | xargs -0 sha256sum > staged-inputs.sha256)
    mv -T "$stage" "$evidence/staged"
    exit
fi

test "${1:-}" = schedule
test -s "$evidence/staged/revision"
until [[ -s "$prior/s50/finished.json" ]]; do sleep 30; done
# The predecessor is a retained transient service and can stay active/exited.
while [[ $(systemctl --user show twins-paper-review-cuda.service -p MainPID --value || true) =~ ^[1-9][0-9]*$ ]]; do
    sleep 15
done
python3 - "$review" "$evidence/staged/gates.json" <<'PY'
from pathlib import Path
import json
import sys
review, target = map(Path, sys.argv[1:])
files = sorted((review/'cuda-results').glob('*/acceptance.json'))
data = json.loads(files[-1].read_text()) if files else {}
cases = data.get('cases', {})
matrix = [x for key, x in cases.items() if key.startswith('irn/')]
target.write_text(json.dumps({
    'acceptance': str(files[-1]) if files else None,
    'irn': len(matrix) == 32 and all(x['accepted'] for x in matrix),
    'ai': all(cases.get('ai/'+x, {}).get('accepted') for x in ('small', 'large', '128')),
}, indent=2)+'\n')
PY
stamp=$(date -u +%Y%m%dT%H%M%SZ)
local_out="$evidence/results/$stamp"
mkdir -p "$local_out"
"${ssh_cmd[@]}" "mkdir -p '$target/project'"
rsync -a -e 'ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=10' "$evidence/staged/" "s50:$target/project/"
until [[ $("${ssh_cmd[@]}" 'nvidia-smi --query-compute-apps=pid --format=csv,noheader' || printf unknown) == '' ]]; do sleep 20; done
set +e
"${ssh_cmd[@]}" "cd '$target/project' && flock /home/cassius/Videos/twins-isolated-20261118/out/exclusive-campaign.lock \
  timeout 4h python3 experiments/paper_review/observe_cuda.py 'runs/$stamp' bash experiments/paper_review/s50_followthrough.sh remote 'runs/$stamp'" > "$local_out/worker.log" 2>&1
rc=$?
set -e
printf '%s\n' "$rc" > "$local_out/remote.rc"
rsync -a --checksum -e 'ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=10' "s50:$target/project/runs/$stamp/" "$local_out/"
"${ssh_cmd[@]}" "set -o pipefail; cd '$target/project/runs/$stamp' && find . -type f -print0 | sort -z | xargs -0 -r sha256sum" \
  > "$local_out/returned-files.sha256"
test -s "$local_out/returned-files.sha256"
(cd "$local_out" && sha256sum --quiet -c returned-files.sha256)
python3 experiments/paper_review/s50_review_results.py "$local_out" "$prior" "$review"
printf '%s\n' "$stamp" > "$evidence/last-return"
exit "$rc"
