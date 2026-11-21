#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
root=$(pwd -P)
local_data=$root/../twiNS
evidence=$root/out/review/release-cuda-20261122
target=/home/cassius/Videos/twins-release-cuda-20261122
ssh_cmd=(ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=20 s50)

if [[ $1 == remote ]]; then
    d=$(realpath -m "$2")
    mkdir -p "$d"
    ulimit -c 0
    ln -sfnT /home/cassius/Videos/twins-extensions-20261119/out/astra out/astra
    cmake -S . -B build -G Ninja -DTWINS_GPU_BACKEND=CUDA -DCMAKE_CUDA_ARCHITECTURES=120 \
        -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc -DCMAKE_EXPORT_COMPILE_COMMANDS=ON > "$d/build.log" 2>&1
    cmake --build build --target twins twins-rdma twins-ai twins-island twins-astra-stub -j2 >> "$d/build.log" 2>&1
    cp build/compile_commands.json "$d/"
    python3 experiments/paper_review/release_regression.py . paper-inputs inventory.json build "$d/replay" \
        --revision "$(cat revision)" --workers 16 > "$d/progress.log" 2>&1
    exit
fi

mkdir -p "$evidence"
if [[ $1 == stage ]]; then
    d="$evidence/staged"
    mkdir -p "$d"
    git archive "$2" CMakeLists.txt src validation | tar -x -C "$d"
    mkdir -p "$d/experiments/paper_review" "$d/out"
    cp experiments/paper_review/{release_cuda_schedule.sh,release_regression.py,observe_cuda.py} "$d/experiments/paper_review/"
    git rev-parse "$2" > "$d/revision"
    python3 - "$root" "$local_data" "$d" "$target/project/paper-inputs" <<'PY'
import json,sys
from pathlib import Path
root,data,stage,remote=map(Path,sys.argv[1:])
data=data.resolve()
sys.path.insert(0,str(root/'experiments/paper_review'))
from release_regression import ai_cases
rows=json.loads((data/'out/paper-evidence-20261116/manifests/scenario-inventory.json').read_text())
needed={data/'validation/simai/jobs_paper.txt'}
for row in rows:
    if row['kind'] not in ('tcp','rdma') or not row['reference_sha256'] or row['case'].startswith('dons_'):
        continue
    reference=Path(row['reference'])
    needed.add(reference)
    row['local_ref_before_staging']=str(reference)
    row['reference']=str(remote/reference.relative_to(data))
    for arg in row['args']:
        if '=' in arg:
            possible=data/arg.split('=',1)[-1]
            if possible.is_file():
                needed.add(possible)
for suite,name,topo,work,ref,extra,cache in ai_cases(data):
    needed.update({data/'data/simai'/topo,work,ref.with_suffix('.ref'),ref.with_suffix('.dag')})
    if cache:
        needed.add(cache)
assert all(path.is_file() and path.is_relative_to(data) for path in needed)
(stage/'inventory.json').write_text(json.dumps(rows,indent=2)+'\n')
(stage/'files.txt').write_text(''.join(f'{p.relative_to(data)}\n' for p in sorted(needed)))
print('Files:',len(needed),'Bytes:',sum(p.stat().st_size for p in needed))
PY
    exit
fi

test "$1" = schedule
while systemctl --user show twins-paper-review-cuda.service twins-paper-review-followthrough.service \
        twins-paper-gpu-pair.service twins-paper-simai-reference.service -p MainPID --value | rg -q '^[1-9][0-9]*$'; do
    sleep 30
done
free=$("${ssh_cmd[@]}" 'df -PB1 "$HOME/Videos" | tail -n 1 | awk '"'"'{print $4}'"'"'')
if (( free < 16*1024*1024*1024 )); then
    printf 'Insufficient free disk before staging: %s bytes\n' "$free" >&2
    exit 75
fi
d="$evidence/staged"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
dst="$evidence/results/$stamp"
mkdir -p "$dst"
"${ssh_cmd[@]}" "mkdir -p '$target/project/paper-inputs'"
rsync -a -e 'ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=10' "$d/" "s50:$target/project/"
rsync -az --files-from="$d/files.txt" -e 'ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=10' \
    "$local_data/" "s50:$target/project/paper-inputs/"
rc=0
"${ssh_cmd[@]}" "cd '$target/project' && flock /home/cassius/Videos/twins-isolated-20261118/out/exclusive-campaign.lock \
   timeout 24h python3 experiments/paper_review/observe_cuda.py 'runs/$stamp' bash experiments/paper_review/release_cuda_schedule.sh remote 'runs/$stamp'" \
   > "$dst/worker.log" 2>&1 || rc=$?
printf '%s\n' "$rc" > "$dst/remote.rc"
rsync -a --checksum --include='*/' --include='*.json' --include='*.log' --exclude='*' \
    -e 'ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=10' "s50:$target/project/runs/$stamp/" "$dst/"
"${ssh_cmd[@]}" "set -o pipefail; cd '$target/project/runs/$stamp' && find . -type f -name '*.json' -o -type f -name '*.log' | sort | xargs -r -d '\n' sha256sum" \
    > "$dst/returned-files.sha256"
test -s "$dst/returned-files.sha256"
(cd "$dst" && sha256sum --quiet -c returned-files.sha256)
exit "$rc"
