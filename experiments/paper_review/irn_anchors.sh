#!/usr/bin/env bash
set -euo pipefail
exe=$(realpath "$1")
fixtures=$(realpath "$2")
out=$(realpath -m "$3")
for name in one cross ecn moderate incast scale; do
    case_dir="$out/$name"
    mkdir -p "$case_dir"
    cp "$fixtures/$name/config" "$case_dir/config"
    (
        cd "$case_dir"
        timeout 300 "$exe" config --topo="$fixtures/graph" --flows="$fixtures/$name/flows.csv" \
            --out=gpu.fct --txtrace=gpu.tx --from=2000000000 --until=2002000000 \
            --fp-bases=65537,1048573 > gpu.log 2>&1
    )
done
