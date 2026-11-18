# Isolated-setting evaluation requested by Astra, 2026-11-18

This round is separate from `paper-evidence-20261116`. It uses the already verified
`6bc5a7c` candidate CUDA binaries, the same patched sequential SimAI, and MTP built
from an identical source snapshot with `NS3_MTP=ON`. No model code is changed.
The TCP reference gets print precision and a post-Run clock log only, linked to
the existing Unison libraries. Source/ELF/library hashes and commands are retained.

`freeze_inputs.py` freezes 8 CUBIC cases and the distinct deduplicated 138/1260
TCP scans. The 1260 historical CSVs originate from the old TwiNS sweep, not new
independent CPU references; the new CPU executions provide the independent check.
Canonical six-field SHA-256 records permit direct current CPU/GPU cross-checking.

`prepare_remote.py` builds private references under `~/Videos/twins-isolated-20261118`.
`runner.py` executes pilots, CUBIC, TCP scans, AI latency/ablations, MTP, then AI
throughput. All measured settings share one exclusive controller lock. There is
no overlap between different measured settings, including CPU-only settings.
CPU multi-process concurrency is internal to one measured throughput setting.

- CUBIC: 8 cases x (seq, Unison1/2/4/8/16/32, GPU) x3 =192 observations.
  Min-of-three event-loop time, all long-frame passes included for GPU. Row-wise
  match, retransmissions, history/capacity and final simulation horizon are separate.
- TCP138: CPU16/32, GPU batch1/138, three batches each =12 batches.
- TCP1260: CPU16/32, GPU fixed upper cap32, three batches each =9 batches.
  Both scan timers include routing, initialization, all retries and output writes;
  validation is outside the batch timer. Explicit128B retries split only if needed.
- AI latency: 8 cases x3 engines x3 rounds =72, plus9 ablations (default reused).
  Process wall includes initialization/stub. Existing GPU/system/sync/worker
  counters and EndToEnd iteration CSVs are preserved, not added together as wall.
- AI throughput: same job order and frozen caps as before, 70GiB,18 batches144 jobs.
- MTP: 3 full128 workloads x6 thread counts =18 initial observations. Per workload,
  choose the fastest completed eligible observation and add two repeats. Crashes,
  incomplete runs, and2-hour timeouts remain evidence; completion and equality
  are distinct. The `-t1` MTP scheduler is not the `NS3_MTP=OFF` sequential control.

Before each setting observe a short idle window: GPU<8%, aggregate background
CPU<one logical core, sufficient available memory. This is observed quiescence,
not an OS reservation; desktop and foreign work are never stopped. Resource
samples include `nvidia-smi pmon` because `query-compute-apps` on this host omits
some external native CUDA tasks. Any sampled foreign GPU work>=20% invalidates
timing. Contaminated attempts retain their receipts and get new retry labels.
The controller repeats missing eligible timings, never silently counts them as3.

`archive_remote.py` hashes only completed exports/build records. `sync_local.py`
pulls only export/receipts with bounded bandwidth and verifies hashes locally.
Full packet traces, raw SimAI detailed CSVs and validation/reference caches stay
remote; matching raw outputs are removed after validation. Important input
manifests, summaries, commands, timing, hashes and necessary failure examples are
local. The live reporter writes only the new materials page, preserving Astra's
ongoing TODO and paper edits. No LaTeX edits, main merge, tag or publication.

The prior measured times imply about68 hours for the two AI groups when run one
setting at a time. TCP, MTP and shared-host waits are additional. Plan in days,
refresh from actual new measurements; do not promise an overnight completion.
