# TwiNS 11/27 experiments

The frozen inputs are the original CUBIC Short flow table, the stock ConWeave IRN reference from the boundary campaign, the 128/1024-host TCP option fixtures, and the 16,000-host strong incast from the full-review campaign. The local raw outputs live in `out/residency-review/`. The acceptor also reads the reference archives alongside that directory.

| Script | Evidence |
| --- | --- |
| `short.sh native\|hip\|cuda EXEC FLOWS DEST` | Eight-field enqueue trace through 100 ms and the six FCT fields |
| `rdma_gpu.sh EXEC INPUTS DEST` | Seven IRN cutoff runs, a partial-slot cutoff, and fixed/automatic 63/64/65-timer probes |
| `dense_native.sh` | Sequential IRN reference for the three timer counts, using `dense.py` to generate matching inputs |
| `rdma_cutoff_controls.sh EXEC INPUTS DEST` | Default, drain, and cutoff coincident with a completion callback |
| `dcqcn.sh EXEC INPUTS DEST` | Frozen DCQCN 2 µs regression with 1 µs execution slots |
| `residency.sh EXEC ANALYZER TCP_INPUTS DEST` | Per-frame timestamps and tail fingerprints on the frozen 10,240-flow workload |
| `profile.py tcp WIDE SPLIT TCP_INPUTS DEST` | Three interleaved invocations per layout with PID-attributed peaks. Use `--tcp-sack 0` for timestamps only |
| `tcp_mixed.sh WIDE SPLIT TCP_INPUTS DEST` | Complete enqueue traces and per-host timer order for DCTCP/CUBIC, with timestamps alone and both options |
| `tcp_retry_tail.py SPLIT TCP_INPUTS DEST` | Measured restart after the 4,096-entry arena fills, using 8,192 on the second attempt |
| `profile.py rdma EXEC EXEC INCAST_INPUTS DEST --compare-reuse` | Three interleaved invocations per RDMA mode, with topology phases, PID high-water marks and wire traces |
| `verify.py out/residency-review` | Acceptance against frozen traces and summaries. Writes `acceptance.json` into the result root |

For the TCP executables, use `TWINS_TCP_OPTIONS`, `TWINS_LINK_TIMING`, `TWINS_TCP_TIMER_ORDER` and `TWINS_CHAIN_RUNS=127`. For arrival residency, add `TWINS_TCP_RESIDENCY` and `TWINS_TCP_BUCKET_AUDIT`. Build `residency.cc` as a C++17 host executable. Its interval sweep processes removals before equal-time arrivals and measures the multiplicity of full stored suffix fingerprints. The 31-run prefix carries the two root words separately.

For the split candidate, add `TWINS_TCP_SPLIT_ARRIVALS` and set `TWINS_TCP_ARRIVAL_TAIL_CAP=8192` on the large input. The scratch space decodes the current arrival slot before the usual host and switch processing. The timed candidate has no per-frame instrumentation. For the two mixed-link trace executables, add `TWINS_TCP_TRACE_TIMERS`. The driver records selected timer entries and whether each was still current. Global GPU print interleaving may vary, so the acceptor checks order within each host. Enqueue order is checked against the sequential reference independently.

Packet RDMA uses an exclusive absolute `--stop-ns` and optional `--drain`. `--until` remains the inclusive logging filter. A cutoff run uses single-slot dispatch. `--due-cap=64` preserves an explicitly bounded run, and the default restarts with a larger per-host scratch reservation on a verified overflow. `--reuse-topology` is an opt-in single-world queue recovery experiment. It uses a sealed anonymous descriptor to retain prepared routing and input tables while each restart rebuilds all mutable state.

Wall times include child setup, retries and result output. The strong-incast world phase includes input parsing and ordered route construction. Initialization includes host staging and device allocation/upload, followed by a separate graph phase and the event loop. PID RSS and the anonymous topology snapshot size are reported separately. Timed runs collect only the process's GPU footprint. The long TCP audit's write sizes describe logical stores and copies.
