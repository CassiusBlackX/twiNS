# Follow-up acceptance experiments

These experiments use the two fixtures that remained in the 11/27 review.
The original frozen inputs, older comparison failures, and adjudicated runs
are in the workspace's `out/unified-review` and `out/post-acceptance` archives.

## Optimized RX TCP split arrivals

The losing build used ROCm Clang 22, `-O2 -fno-inline-functions`, the 127-run
short-prefix target, and the 1,024-host status/arrival fixture in
`out/tcp-followup-20261126/inputs`. The same compiler and input cross the
previous failing slot after the timer/arrival comparator reuses the host
event-chain workspace. Every branch that picks the next event initializes
that workspace before dispatch. This removes the second wide chain allocation
from device private memory.

The regular CMake target is `twins2048-options-split-links`, selected by
the normal `twins` retry driver. Use the PID sampler to repeat the same input.

```sh
python3 experiments/paper_unified/tcp_storage.py hip build/twins \
    out/tcp-followup-20261126/inputs trial-1 --skip-batch
python3 experiments/paper_unified/tcp_storage.py hip build/twins \
    out/tcp-followup-20261126/inputs trial-2 --case default --case preset --skip-batch
```

Use a split build with `TWINS_TCP_TRACE_TIMERS` for
`tcp_queue_audit.sh BINARY INPUTS OUT` on either GPU. Its output matches
the original 128-host DCTCP and CUBIC fixtures, timestamps only and both
options on. The audit compares six flow fields, queue event order per port,
and timer order per host against the previously accepted archives.

## Exact RDMA packet roots

The original author binary chooses its BDP from the topology basename. The
native bridge preserves the topology byte for byte and presents its original
name. It stably sorts the flow-input rows by their original exact time so
ns-3 can stream them in order. The optional observer logs the actual creator
timestamp, scheduled timestamp and ns-3 group UID, plus flow application
installation order.

```sh
python3 experiments/paper_fixes/native_roots.py ORIGINAL_DRIVER STOCK_LIBS reference-build
python3 experiments/paper_unified/loss_holdout.py native reference-build/reference \
    out/unified-review/loss-holdout native --libraries STOCK_LIBS --native-order
TWINS_RDMA_ROOT_TRACE=1 python3 experiments/paper_unified/loss_holdout.py gpu \
    build/twins-rdma out/unified-review/loss-holdout candidate
python3 experiments/paper_fixes/verify_irn_roots.py \
    out/unified-review/loss-holdout/flows.csv native candidate
```

Root ranks for ordinary packet flow files use the known integer
`ScheduleFlowInputs` delay before compression and stable install order for
equal delays. Traced DAG roots retain their separate encoded comparison and
terminal checks. The verifier keeps the old flagging run as an optional
negative and checks the new exit status separately from packet and FCT parity.
