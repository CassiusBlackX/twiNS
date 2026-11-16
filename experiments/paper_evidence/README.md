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

`TWINS_EVIDENCE` is off by default. Histograms are push/comparison weighted log2 buckets (bucket 0 includes 0/1), not distributions over distinct resident histories. RDMA full RLE run counts sample every 4096 pushes. The final counter build uses separate per-tail depth metadata rather than traversing the full tail at each sample; unknown depths/overwrites are counted separately. Its extra allocation is reported as instrumentation, not model state. Earlier traversal-based samples are retained only as intrusive profiling evidence. TCP history lengths are recorded-prefix lengths when truncation is flagged. Tail reuse counts are retained tail pointers at pushes, not a census of all live reference counts.

`evidence_counts` indices: 0 pushes; 1 sum of observed lengths; 2 sum of inline runs; 3 RLE extensions; 4 new runs; 5 pushes retaining a tail; 6 truncated-prefix observations; 7 maximum observed length; 8 sum walk steps; 9 measured walks; 10 valid full-RLE samples; 11 sampled full-run sum; 12 corresponding length sum; 13 lost-tail samples; 14 max sampled full-run count; 15 tail metadata reads (v1 traversals, v2 constant-time depth lookups; see evidence_schema).

The explicit allocation ledger is monotone (the drivers do not free device allocations before process exit). `tracked_device_allocated` is the high water of tracked allocations, not total driver/runtime memory. `inline_allocated` covers explicit frame/Chain/history arrays; the TCP switch last-chain allocation must also be included when deriving complete storage categories. Tail allocation count is lifetime nodes created; ring high water is `min(created, slots)`. Allocation wraps do not imply a decision actually read a lost tail. Whole-device VRAM samples include the desktop/other users and are separately labeled.

## Candidate fixes and parameter contracts

The MMU candidate additionally checks ingress admission: for ingress byte bound A and shared-ingress bound U, either the port's headroom admits every possible packet (`headroom >= A`), or `A + U <= threshold_lower_bound`. The original PFC-only test admitted a first packet that the sequential oracle dropped. Preserve unsigned buffer subtraction and the signed 64-bit `S_max < Q` guard. Packet payload plus headers must fit `Pkt::size` (16 bits); overflowing headroom arithmetic is refused. MMU reasoning assumes coherent state and counters within their specified integer ranges; synthetic inconsistent arithmetic states are not claims of reachability. The finite selected tests do not prove all topology/parameter combinations.

`TWINS_TCP_TIMER_ORDER` is a separate, default-off candidate. Timers scheduled by one host inherit its local monotone scheduling sequence. If their chains tie, their known unequal creation sequences recover relative reference order; saturated/unknown keys fall back to the existing flag behavior. It does not resolve arbitrary frame-history truncation. It adds six 64-bit keys per connection and one counter per host, without changing frame size. Main/grid regressions and measured memory/cost are required before promoting it.

Saturated 32-bit RDMA run counts remain unknown: virtual pushes clamp, and an exact walk that needs to pass such a run returns undecided for the caller to flag. The observed length counter is capped and is never a probability budget.

`TWINS_NO_FP` disables the FP equality shortcut but continues updating/carrying the pair. `TWINS_HASH_MEMO`, `TWINS_LSID`, `TWINS_NO_SEQLOCK`, no-chain/lean/UB/mutation experiments are not accepted paper evidence builds.

## Evidence versions and completion

The final controlled A2 source is `48e0a3a`, archived with per-file hashes in `frozen-candidate.json`; its binaries in `build-final-*` are never rebuilt during the campaign. `6bc5a7c` adds conservative UINT32_MAX prefix handling after that evidence freeze. Candidate HIP/CUDA checks are independent and must pass before calling it validated. The optional microbenchmark defines its own corpus types; no corpus instrumentation is enabled in the default candidate simulation builds.

`derive_a2.py` produces JSON, Markdown and standalone PDF figures with receipt paths and hashes. It subtracts profiling-only64MiB from tracked model allocation, and adds the TCP switch last_chain allocation omitted by the initial instrumentation ledger. RLE, history length and comparison distributions are push/comparison weighted; retained pointers do not measure distinct live sharing. The main two AI workloads have only about1.08× full-history RLE reduction.

`archive_index.py` hashes completed exported evidence. `sync_results.py` verifies the local copies against that index. It never copies `remote-raw/`. `execution_status.py` keeps a cross-host Markdown status page until both pipelines and candidate gates have returned results and the indexed archive is verified. A stage returning does not imply scientific acceptance, and no script merges main or publishes a tag.

The throughput cap policy is `max(1GiB, ceil(1.10 * max accepted A3 cgroup peak + 512MiB))`, frozen before batch1 with a70GiB reservation budget. Batch validation and provenance are outside actual makespan. `batch_selfcheck.py` exercises real subprocess overlap, cap admission and the validation timing boundary; `orchestration_selfcheck.py` checks the three-consecutive-sample resource classifier. Neither is simulation evidence. Own Docker cleanup uses campaign/owner/role labels after obtaining the campaign lock.

Local execution uses user services with MemoryMax28G and swap allowed. `hip_candidate.sh` waits for local evidence stages to end, builds a separate HIP candidate and checks saturated-prefix handling with a finite positive control. Existing48e0 outputs are not relabeled. `mmu_boundaries.py` supports `TWINS_MMU_REPRO_OUT=/tmp/...` for isolated reproduction, followed by `validate_mmu_tx.py` for all ten TX fields and per-link order.

## Parallel scheduling revision, authorized2026-11-16 afternoon

`parallel_latency.py` takes over only after the pre-existing measurement returns. While that CPU reference runs on core0, new references may already run on cores8/4/12. The controller and observers use core15. New CPU cgroup caps are1.10× the largest accepted observed peak+512MiB, rounded up; the shared reservation budget is70GiB and P0 reserves10GiB host RAM. P0 retains core0 and a single GPU. P16 retains0–16 and runs after the own CPU queue drains. No production binary, input, fixed FP base, reference or output check changes.

`parallel_batches.py` executes the same18 true batches with disjoint physical core pools. Each batch reserves a conservative bound: the largest possible sum of its remaining per-job caps at its target concurrency, capped at70GiB. A batch releases only quotas it can never need again; other batches do not delay its internal admission. P0 may overlap CPU batches; P16 is exclusive among this campaign's models. GPU models never overlap each other. Cancelling the controller prevents workers from starting new jobs and stops only campaign-owned containers.

These are shared-host measurements. The resource epoch, CPU masks, changed upper memory limits, active peers and quota history are recorded in `resource_context` and `export/parallel/`. Do not describe all repetitions as isolated whole-machine runs or infer an isolated-performance confidence interval. Preserve old serial observations and all new observations; the same10% variation/interference supplement rule remains active. Important parallel dispatch and policy records are included in the local archive, not the bulk traces.

`parallel_latency_selfcheck.py`, `parallel_selfcheck.py` and `parallel_dispatch_selfcheck.py` use actual short-lived sleeping subprocesses to exercise overlap, P16 barriers, safe handoff, decreasing memory bounds, core separation and all18 batch dispatches. They are scheduler checks, not scientific performance measurements.
