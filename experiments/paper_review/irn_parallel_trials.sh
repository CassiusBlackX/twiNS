#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root"
binary=$(realpath "$1")
prior=$(realpath "$2")
dest=$(realpath -m "$3")
mkdir -p "$dest/build"
ln -s "$binary" "$dest/build/twins-rdma"
for trial in 1 2 3; do
  if ((trial % 2)); then modes=(serial parallel); else modes=(parallel serial); fi
  for mode in "${modes[@]}"; do
    flag=--serial
    [[ $mode != parallel ]] || flag=--irn-parallel
    python experiments/paper_scale/gpu.py "$prior/dcqcn/inputs" "$dest/timing/$trial/$mode" \
        "$dest/build" --hosts 128 1024 --gpu-option="$flag"
  done
done
for mode in serial parallel; do
  flag=--serial
  [[ $mode != parallel ]] || flag=--irn-parallel
  python experiments/paper_scale/irn_diag.py "$prior/dcqcn" "$binary" gpu \
      --out "$dest/wire/$mode" --gpu-option="$flag"
done
