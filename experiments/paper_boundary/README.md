# TwiNS boundary experiments

The run directory holds frozen inputs and separate receipts for HIP, CUDA and native references. Use the local GPU with the 28 GiB user service limit. a87 serves the bounded native runs and HIP builds. CUDA runs share the s50 campaign lock.

| Case | Reproduction entry |
| --- | --- |
| Closed-loop termination and cache reuse | Run `closed_cases.py` with the same frozen 6 MiB collective input. `closed_last.sh` cuts off the last coordinator round after all network flows complete. `cache_endpoints.sh` changes the collective group size on the same cache path and compares against a cold run. |
| CUDA process memory | `pid_memory.py` runs three unprofiled and three PID-sampled, interleaved pairs. `memory.sh` selects the prior executable. `memory_phases.sh` builds companion binaries that record explicit allocations and available memory around graph initialization. |
| CUBIC drain | `cubic-native.sh` and `cubic_gpu.sh` use the frozen A2A30 input and a common absolute 10 ms cutoff. The native enqueue trace is stably sorted by timestamp, node and device. |
| Completion ordering | `hpcc_close.py` records native lifecycle order and compares an independent control that skips post-completion handlers. `simai_life.py` relinks an isolated native library for the live two-layer collective. `closed_probe.sh` needs a build with `RDMA_LIFETIME_PROBE`. `compare_simai.py` checks full feedback, normalized actions, and FCTs. |
| Short IRN recovery | Stage the pinned stock ConWeave headers and libraries under `conweave-stock`. `irn_stock.py` exposes the existing low timer as a probe parameter. For the same-time case set `TWINS_IRN_FIRST_SIZE=970` and use 833/834/835 ns in both runners. `compare_irn.py` uses a cutoff at the common final flow completion. `irn_regression.py` matches the existing accepted 32-case inventory. |
| Per-port queue growth | `cuda-run.sh` uses the unchanged 16,000-host strong incast with the normal driver's opt-in `--q3-auto` path. Match its final FCT with the compact native reference and check the total wall clock, including every retry. |

The intermediate attempts with the wide ConWeave packet layout and the separate audit object are retained in the run archive. The accepted short-RTO comparison uses the stock 1,048-byte frame layout.
