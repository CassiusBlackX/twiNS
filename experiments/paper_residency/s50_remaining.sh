#!/usr/bin/env bash
set -euo pipefail
old=/home/cassius/Videos/twins-isolated-20261118/out
test -s "$old/export/all-finished.json"
base=/home/cassius/Videos/twins-full-review-20261126
bin=twins-paper-tcp-options/build
wait_build() {
    for ((i=0;i<120;i++)); do
        [[ -f "$bin/$1.rc" ]] && break
        sleep 5
    done
    test "$(cat "$bin/$1.rc")" = 0
}
exclusive() (
    exec 9>"$old/exclusive-campaign.lock"
    flock 9
    while [[ -n $(nvidia-smi --query-compute-apps=pid --format=csv,noheader) ]]; do sleep 5; done
    "$@"
)
wait_build rdma-final
exclusive python3 profile.py rdma "$bin/rdma-final" "$bin/rdma-final" "$base/rdma-candidate/inputs/k40" \
    strong-formal --compare-reuse
exclusive bash rdma_gpu.sh "$bin/rdma-final" . rdma-cuda-final
wait_build tcp-audit
TWINS_TCP_ARRIVAL_TAIL_CAP=8192 exclusive bash tcp_mixed.sh "$bin/tcp-wide-audit" "$bin/tcp-split-audit" "$base/tcp-inputs" tcp-mixed-cuda
exclusive python3 tcp_retry_tail.py "$bin/tcp-split" "$base/tcp-inputs" tcp-tail-retry
