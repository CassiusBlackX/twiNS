# Validation against ns-3

## The ns-3 reference

`ns3/twins-ref.cc` is the reference scenario for ns-3.46.1. It builds a FatTree or a leaf-spine
(`--topo=leafspine --leaves --spines --hpl`), reads the same flow files as TwiNS, and gives
every flow its own TCP connection. FCT is recorded from connection establishment to the ACK of
the last byte. Each output row also carries ns-3's two ECMP flow hashes (`hash_data,hash_ack`),
which TwiNS takes with `--ecmp_from`, plus the flow's retransmission count.

Parameters (aligned with GeDES, so its model can be compared too):

| parameter | setting |
|---|---|
| links | 100 Gbps, 1 µs propagation delay, MTU 1500 / MSS 1460 |
| switch egress | 200 packets (`--queue`), RED with MinTh = MaxTh = K = 30 (`--ecnK`) and QW = 1, so ECN marks on the instantaneous queue |
| TCP | initial cwnd 10, min RTO 1 ms, no SACK, no timestamps, classic recovery, no limited transmit |
| ECMP | `--flowEcmp 1`: per-flow hashing; equal-cost routes ordered by interface |
| host egress | `--hostq red`: the same RED queue disc as the switches |

The references must be run **sequentially** (no `--threads`). Exactness is defined against
sequential ns-3's event order.

**Routing patch.** `ns3/ipv4-global-routing.patch` changes ns-3's Ipv4GlobalRouting in two ways:
- It orders equal-cost routes by output interface, so a flow hash maps to a reproducible port.
- It indexes the host and network routes.
  - Without the index, set-up is quadratic: at k=16 it ran for more than 40 minutes without
    finishing, because ns-3 deduplicates and looks up routes by scanning whole tables. Every
    packet's lookup also scans thousands of routes.
  - The index holds the same entries in the same order, so lookups return identical results.
  - Rerunning the k=8 references with it reproduced their outputs exactly, about 5× faster.
  - All ns-3 timings in the README use the patch.

**Setting up ns-3.**

```shell
cd ns-3.46.1 && patch -p1 < .../validation/ns3/ipv4-global-routing.patch
cp .../validation/ns3/twins-ref.cc scratch/
./ns3 configure -d optimized && ./ns3 build scratch/twins-ref
```

The run scripts in `ns3/` work in `$NS3_WORK` (default `~/netsim/cmp`). That directory holds:
- `flows/`, a copy of `data/flows`;
- `ref/` and `sweep/`, the outputs.

`$NS3_BIN` names the scenario binary (default
`../unison/build/scratch/ns3.46.1-twins-ref-optimized`).

| script | runs |
|---|---|
| `run_ref.sh` | k=8, DCTCP/NewReno, base flows and the first jittered variant (generates `*_jit.csv`) |
| `run_noise.sh` | k=8, DCTCP/NewReno, jitter variants 1–4 (generates `*_jit1..4.csv`) |
| `run_cubic.sh`, `run_cubic_jit.sh` | k=8 Cubic (other workloads via `WL=...`), and its jittered variants |
| `run_k16.sh` | k=16, DCTCP/NewReno |
| `run_ls.sh` | leaf-spine, 3 TCPs |
| `run_sweep.sh`, `run_sweep16.sh` | the k=8 and k=16 sweeps, 32 configurations in parallel (`sweep_one.sh`) |

**Debug traces in the scenario** (environment variables):
- `TRACE_FLOW=<id>`: one flow's segments, ACKs, cwnd and DCTCP's alpha updates.
- `TRACE_ALLQ=<ps>`, with optional `TRACE_ALLQ_FROM=<ps>`: every enqueue in the network up to
  that time.
- `TRACE_RX=<dst>,<src>,<ns>`: the receiving socket.
- `TRACE_QUEUE`, `TRACE_NODE`, `TRACE_DEV`: enqueues at chosen queues.

## TwiNS against ns-3: identical

With ns-3's hashes (`--ecmp_from`), TwiNS gives every flow the same establishment and
completion time as ns-3 in all of these runs:

| set | flows | TCP | runs | identical flows |
|---|---|---|---|---|
| k=8: paper, fullsync, fullexp, a2a30, incast16, a2a50s × 6 variants | 128 to 12519 | DCTCP, NewReno, Cubic | 108 | all |
| k=16: paper, a2a30, incast16, a2a50s | 160 to 50818 | DCTCP, NewReno, Cubic | 12 | all |
| leaf-spine 16×16, 8 spines: paper, a2a30, incast16, a2a50s | 160 to 25390 | DCTCP, NewReno, Cubic | 12 | all |
| sizes not MSS multiples: k8 a2a30b, a2a50b, incastb; ls a2a30b; k16 a2a30b | 288 to 2280 | DCTCP, NewReno, Cubic | 15 | all |
| persistent connections (`conn`): k8 r30, r50, r40p1, rpc; ls r30; k16 r30; each with and without FIN; FIN on k8 a2a30, incast16, a2a50s, a2a30b | 157 to 25344 | DCTCP, NewReno, Cubic | 48 | all |
| per-packet spraying: k8 paper, a2a30, incast16, a2a50s, a2a30b; ls a2a30; k16 a2a30; k8 r30 with FIN | 128 to 12519 | DCTCP, NewReno, Cubic | 24 | all |
| topology files: Jellyfish 32 sw (a2a30, a2a50, incast), torus 6×6, Dragonfly 4×9, Jellyfish 256 sw; flow ECMP and spraying | 289 to 2316 | DCTCP, NewReno, Cubic | 48 | all |
| link failures/recoveries: FatTree k=8 file (2 workloads), Jellyfish, torus; flow ECMP and spraying | 273 to 12519 | DCTCP, NewReno, Cubic | 24 | all |
| k=8 sweep: a2a30, a2a50w × K × queue limit | 273, 576 | DCTCP, NewReno, Cubic | 138 | all |
| k=16 sweep: a2a30, a2a50w × K ∈ {10,20,40,80} × queue ∈ {100,400} | 2253, 3788 | DCTCP, NewReno, Cubic | 48 | all |

That is 477 runs; `check_all.sh` reruns them all. Some need `twins128` (see below).

**New scenario options** (`ns3/twins-ref.cc`): a fifth flow-file column `conn` (messages over one
connection, written at their start times), `--fin=1`, `--flowEcmp=0` with deterministic routing
streams (stream = node id), `--topo=file --topofile=F` (and `DUMP_ROUTES=path` to print the
equal-cost routes), `--fail=T:sA:sB[:up],...`. `ns3/run_jobs.sh` runs reference jobs from a job
file (`ns3/jobs_*.txt`). `tools/rng_ref.cc` and `tools/rng_check.cu` check src/rng.h against
ns-3's MRG32k3a.

**Undecided ties.** A frame records 9 runs of its event history. In symmetric situations (spraying
over equal paths, large sweeps) two simultaneous events can have equal histories over all 9; TwiNS
then counts an undecided tie and warns. `twins128` records 25 runs; `check_exact.sh` and
`bench/sweep.py` fall back to it. In the runs above, it decided every such case that mattered.

They include ECN marking, RED drops, fast recovery, RTOs with backoff, SYN and SYN-ACK
retransmissions, and incast.

- `check_exact.sh` reruns the first three rows.
- `check_sweeps.sh` reruns the sweeps. It runs them as batches of worlds, so it also checks
  that batching does not change results.

**Finding differences.**
1. `twins --qtrace=f [--qtrace_from=ps] --qtrace_until=ps` and ns-3's `TRACE_ALLQ` dump every
   enqueue.
2. `tools/qdiff.py` reports the first difference between the two histories.
3. Read the ns-3 code path behind it.

About 30 corrections were found this way. Some only show in particular configurations:
- *A ring sized for 256 frames.* It silently dropped frames with 400-packet queues. Rings are now
  sized from the largest queue limit.
- *DCTCP's alpha.* It is computed as GCC compiles ns-3 with `-march=native`:
  `fma(1 - g, alpha, g * F)`. This was checked on 2 million random inputs.
- *EnterRecovery.* It retransmits without moving `m_nextTxSequence`, and DCTCP's observation
  window depends on that.
- *Chain comparison.* A complete chain that ends before the other reaches its root, an
  application start scheduled at set-up, so it comes first.
- *Per-flow buffers in batches.* They were sized from the total flow count. So a batch of 16
  k=16 worlds got smaller RTT-history rings than a single world, and a Cubic flow with 8,253
  segments in flight lost RTT samples. They are now sized per flow, from the flow's segment
  count.

**Floating point.** `tools/nsmath_ref.cc` (an ns-3 scratch program) prints reference values of
`Time::GetSeconds()` and of `std::pow` as ns-3 calls it. `tools/nsmath_check.cu` compares
`src/nsmath.h` with them, on the host and on the GPU:

```shell
/opt/rocm/llvm/bin/clang++ -x hip --offload-arch=gfx1200 -O3 -ffp-contract=off -Isrc \
    validation/tools/nsmath_check.cu -o nsmath_check -L/opt/rocm/lib -lamdhip64
./nsmath_check < nsmath_ref.txt     # 0 of 3M pow and 0 of 2M GetSeconds values differ
```

**Unison is not equivalent to ns-3 per flow.** With 4 or 16 threads, Unison gives identical
results to itself. Against sequential ns-3 it differs on every flow of k8_paper DCTCP, because
cross-partition ties are ordered differently. Statistically the difference is noise-sized (mean
FCT 0.6%, p99 4.3%, KS 0.05), like a run with jittered starts.

## The GeDES model against ns-3: far off

`noise.py` reports the error relative to ns-3, next to ns-3's own spread:
- ns-3 noise is measured over pairs of ns-3 runs, as the median/max of the mean-FCT error;
- the GeDES model runs on the base flows (`data/gedes`).

| workload | TCP | ns-3 noise: mean FCT | GeDES model: mean FCT | p99 | KS |
|---|---|---|---|---|---|
| paper | DCTCP | 0.7% / 1.7% | 65.8% | 83.2% | 0.69 |
| paper | NewReno | 2.0% / 4.9% | 75.5% | 84.8% | 0.84 |
| fullsync | DCTCP | 1.0% / 2.2% | 60.7% | 80.3% | 0.98 |
| fullexp | DCTCP | 0.2% / 0.4% | 40.7% | 72.3% | 0.32 |
| a2a30 | DCTCP | 0.7% / 1.5% | 14.9% | 2.4% | 0.07 |
| incast16 | NewReno | 3.5% / 8.7% | 58.6% | 88.0% | 0.77 |
| a2a50s | DCTCP | 1.2% / 1.8% | 91.8% | 95.0% | 0.53 |

GeDES itself has one connection per host. The paper, fullsync and fullexp outputs are GeDES's
own. For a2a30, incast16 and a2a50s, the outputs come from a byte-identical reimplementation of
the GeDES model extended to several connections per host.

GeDES is systematically optimistic:
- Its deterministic per-slot spraying balances symmetric load perfectly. In fullsync, all 128
  flows get the same FCT.
- In the GeDES paper's setup, its "NewReno" and "DCTCP" are byte-identical, because initial cwnd
  equals ssthresh and nothing gets marked or dropped.

**Why per-flow ECMP is the reference.** Per-packet spraying in ns-3 causes spurious
retransmissions through reordering. Raising `ReTxThreshold` does not suppress them.
