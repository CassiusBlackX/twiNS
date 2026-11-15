# Paper evidence campaign, 2026-11-16

This is a private experimental candidate on `paper-evidence-20261116`. It does not merge `main`, publish a release/tag, or edit LaTeX. The original exact-audit working tree and dirty audit document are retained.

The campaign's original A1 snapshot and hashes are in `twiNS/out/paper-evidence-20261116/manifests/`. A3 uses that frozen, log-corrected CUDA build. Later MMU and timer experiments have distinct binary/source hashes; do not relabel the A3 measurements as this later implementation. Final controlled A2 builds use one frozen source revision with only build-definition differences.

## Scripts

- `matrix.json`: eight fixed jobs, reference p64 MD5s, selection rationale, rounds and budgets.
- `campaign.py`: calibrated, interleaved A3 repetitions, exact UID/start/finish cardinality and duplicate checks, documented DAG normalization, nanosecond process wall, resource samples, per-pass flags and bounded timeouts. Fixed bases are reproducibility parameters, not a probability certificate.
- `after_latency.py`: checkpoint after the final ablation; preserve any preempted partial attempt; re-evaluate terminal flags/supplementary repetitions; then start `batches.py`.
- `batches.py`: hash inputs before the batch timer; validate after the measured batch makespan. CPU target concurrency 1/2/4/8; reserve per-job caps within 70GiB. Caps are frozen using observed A3 cgroup peaks before batch 1. P0/P16 process the same eight jobs sequentially. Interrupted batches are entirely repeated with a new trial ID.
- `sync_results.py`: local allowlisted pull of remote `export/` and `receipts/`. No `remote-raw/` or reference sorting caches are copied. Important result/validation/version/resource files remain locally archived.
- `clean_accepted_raw.py`: remove only this campaign's matching, regenerable raw outputs after validation. Failed runs are retained.
- `residuals.py`: six cases at 9/25/57/121 TCP runs (64/128/256/512B), preserving each attempt and cumulative wall. `timer_residuals.py` checks the separate optional timer representation.
- `inventory.py`, `unified_runs.py`: explicit suite memberships/config/reference hashes and new per-attempt default/128B receipts. Historical missing pass costs remain unknown.
- `mmu_fixtures.py`, `mmu_boundaries.py`: arithmetic fixtures and small reachable-state oracle tests. Boundary observations represent the state after events with timestamp `< t`, and comparisons use common observed boundaries before the reference stops. Empty matching FCTs with rc=2 are **stranded**, not fully completed. Keep the original unsafe counterexample and intermediate coverage attempts.
- `profile_runs.py`: clean/count three-round A2 runs; `TWINS_PROFILE_STRICT_ONLY=1` is the separate one-hour-censored exact-walk sweep. Instrumentation failures/overhead are never substituted for clean performance.
- `corpus_runs.py`, `replay_micro.py`, `lineage_bench.cu`: logarithmic-in-comparison-count recorded inputs and GPU microbenchmarks using production `cpush`, `fp_step`, `ccmp_walk` and `ccmp`. The walk includes the root-start prefix; FP covers only the subsequent suffix. Preserve the failed v1 scope check; use only fully checked later results. Tails are reconstructed contiguously and accesses repeated; these timings cannot be assigned directly to production pointer locality or summed as overlapped wall-clock components.

## Observational definitions

`TWINS_EVIDENCE` is off by default. Histograms are push/comparison weighted log2 buckets (bucket 0 includes 0/1), not distributions over distinct resident histories. RDMA full RLE run counts sample every 4096 pushes; lost-tail samples are counted separately. TCP history lengths are recorded-prefix lengths when truncation is flagged. Tail reuse counts are retained tail pointers at pushes, not a census of all live reference counts.

`evidence_counts` indices: 0 pushes; 1 sum of observed lengths; 2 sum of inline runs; 3 RLE extensions; 4 new runs; 5 pushes retaining a tail; 6 truncated-prefix observations; 7 maximum observed length; 8 sum walk steps; 9 measured walks; 10 valid full-RLE samples; 11 sampled full-run sum; 12 corresponding length sum; 13 lost-tail samples; 14 max sampled full-run count; 15 sampled tail nodes traversed.

The explicit allocation ledger is monotone (the drivers do not free device allocations before process exit). `tracked_device_allocated` is the high water of tracked allocations, not total driver/runtime memory. `inline_allocated` covers explicit frame/Chain/history arrays; the TCP switch last-chain allocation must also be included when deriving complete storage categories. Tail allocation count is lifetime nodes created; ring high water is `min(created, slots)`. Allocation wraps do not imply a decision actually read a lost tail. Whole-device VRAM samples include the desktop/other users and are separately labeled.

## Candidate fixes and parameter contracts

The MMU candidate additionally checks ingress admission: for ingress byte bound A and shared-ingress bound U, either the port's headroom admits every possible packet (`headroom >= A`), or `A + U <= threshold_lower_bound`. The original PFC-only test admitted a first packet that the sequential oracle dropped. Preserve unsigned buffer subtraction and the signed 64-bit `S_max < Q` guard. Packet payload plus headers must fit `Pkt::size` (16 bits); overflowing headroom arithmetic is refused. MMU reasoning assumes coherent state and counters within their specified integer ranges; synthetic inconsistent arithmetic states are not claims of reachability. The finite selected tests do not prove all topology/parameter combinations.

`TWINS_TCP_TIMER_ORDER` is a separate, default-off candidate. Timers scheduled by one host inherit its local monotone scheduling sequence. If their chains tie, their known unequal creation sequences recover relative reference order; saturated/unknown keys fall back to the existing flag behavior. It does not resolve arbitrary frame-history truncation. It adds six 64-bit keys per connection and one counter per host, without changing frame size. Main/grid regressions and measured memory/cost are required before promoting it.

`TWINS_NO_FP` disables the FP equality shortcut but continues updating/carrying the pair. `TWINS_HASH_MEMO`, `TWINS_LSID`, `TWINS_NO_SEQLOCK`, no-chain/lean/UB/mutation experiments are not accepted paper evidence builds.
