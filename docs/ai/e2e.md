# Phase 3: TwiNS against SimAI, end to end

**Result.** TwiNS reproduces SimAI end to end:
- Every message's start and completion is identical to the nanosecond, and so is the time of the
  run's last event.
- SimAI here means astra-sim-alibabacloud with ns-3-alibabacloud, single-threaded.
- 16 runs were compared: microAllReduce at 8, 16 and 128 GPUs, and AICB's Megatron (Llama- and
  GPT-like) and MoE workloads at 16 GPUs. Topologies are Spectrum-X, AlibabaHPN (dual ToR) and DCN+,
  up to 520192 messages per run.
- TwiNS replays astra-sim's recorded actions, which makes it a trace-driven system model:
  - Exactness at the system level holds for the recorded run.
  - A different network (another topology or CC) would need astra-sim to run again, since its
    decisions could change.

## Method

1. **Instrumented SimAI** (`validation/simai/trace_patch.py`, tree `simai-trace` on a87). This is
   simai-ref's tree (sequential, `simai.patch`) with three changes:
   - With `TWINS_TRACE` set, every astra-sim action (`SendFlow`, per queue pair, and
     `sim_schedule`) is recorded, along with the ns-3 event it ran in and its ordinal there:
     - the ns-3 event is a QpComplete, a SendComplete, a timer, or main;
     - the ordinal is ns-3's uid counter minus the uid at the event's start.
   - The frontend's setup is made reproducible, as simai-ref's is (see below).
   - Optionally (`TWINS_TXTRACE`), every transmission is traced, in simai-dag-ref's format.
2. **The DAG** (`workloads/trace2dag.py`, `docs/ai/dag.md`): flows, timers, and one cause and
   ordinal per flow and timer. The run's `fct.txt` becomes the reference per message.
3. **Replays.**
   - `simai-dag-ref` replays the DAG on ns-3-alibabacloud. This checks the DAG's semantics
     independently of TwiNS.
   - twins-rdma replays it on the GPU.
   - `validation/simai/check_e2e.sh jobs_e2e.txt` compares TwiNS with SimAI.

## What SimAI does that the references had to follow

Found by diffing transmission traces (SimAI against simai-dag-ref), then printing the inputs of
the first differing decision in both binaries:

- **`GLOBAL_T` is ignored.**
  - `ReadConf` reads it and then sets `global_t = 1`. Every queue pair gets `maxBdp` and `maxRtt`
    as its window and base RTT.
  - simai-ref, simai-dag-ref and TwiNS had honored `GLOBAL_T 0` (per-pair values). They now
    follow SimAI; `GLOBAL_T_HONOR=1` keeps the per-pair path, which `jobs_net1`/`2` still cover.
  - All SimAI references were remade. Two jobs then deadlock under PFC (some flows never finish)
    and stop at 1 ms.
- **ECN marks.** `simai.patch` gives each switch port its own random stream, but SimAI's frontend
  never configures them. Its marks come from a new `UniformVariable` per draw, whose stream depends
  on how many were created before. The traced SimAI configures them as simai-ref does.
- **Topology maps.**
  - SimAI keys `nbr2if`, `nextHop` and the pair maps by `Ptr<Node>`, a heap address. Their order
    decides the order of a switch's ECMP next hops.
  - The traced SimAI orders them by node id, as simai-ref does.
  - On these topologies this changed paths without changing any message's timing.

## Results

| run | GPUs | topology | workload | messages | identical | last event identical | SimAI (s) | TwiNS (s) |
|---|---|---|---|---|---|---|---|---|
| tr_sx8 | 8 | Spectrum-X | microAllReduce | 1792 | 1792 | yes | 2.0 | 0.8 |
| tr_sx16 | 16 | Spectrum-X | microAllReduce | 7680 | 7680 | yes | 3.6 | 1.1 |
| tr_hpn16 | 16 | AlibabaHPN | microAllReduce | 7680 | 7680 | yes | 5.4 | 1.8 |
| tr_dcn16 | 16 | DCN+ | microAllReduce | 7680 | 7680 | yes | 1.8 | 1.0 |
| tr_sx128 | 128 | Spectrum-X | microAllReduce | 520192 | 520192 | yes | 60.4 | 4.3 |
| tr_hpn128 | 128 | AlibabaHPN | microAllReduce | 520192 | 520192 | yes | 62.7 | 5.2 |
| tr_dcn128 | 128 | DCN+ | microAllReduce | 520192 | 520192 | yes | 52.6 | 5.4 |
| a_llama_sx16 | 16 | Spectrum-X | Megatron Llama-7B-like, tp 8 | 517008 | 517008 | yes | 121.4 | 63.6 |
| a_llama_hpn16 | 16 | AlibabaHPN | Megatron Llama-7B-like, tp 8 | 517008 | 517008 | yes | 122.4 | 77.6 |
| a_llama_dcn16 | 16 | DCN+ | Megatron Llama-7B-like, tp 8 | 517008 | 517008 | yes | 120.3 | 43.6 |
| a_gpt13_sx16 | 16 | Spectrum-X | Megatron GPT-13B-like, tp 8, SP | 264336 | 264336 | yes | 95.3 | 58.5 |
| a_gpt13_hpn16 | 16 | AlibabaHPN | Megatron GPT-13B-like, tp 8, SP | 264336 | 264336 | yes | 98.0 | 81.2 |
| a_gpt13_dcn16 | 16 | DCN+ | Megatron GPT-13B-like, tp 8, SP | 264336 | 264336 | yes | 93.2 | 43.9 |
| a_moe_sx16 | 16 | Spectrum-X | MoE, tp 2, ep 8 | 6496 | 6496 | yes | 49.6 | 37.3 |
| a_moe_hpn16 | 16 | AlibabaHPN | MoE, tp 2, ep 8 | 6496 | 6496 | yes | 53.8 | 58.4 |
| a_moe_dcn16 | 16 | DCN+ | MoE, tp 2, ep 8 | 6496 | 6496 | yes | 37.2 | 26.0 |

SimAI is the single-threaded sequential build on a87 (Xeon Gold 5218; up to 9 runs side by side).
TwiNS runs on an RX 9060 XT. "Last event" is the run's end: the last timer firing or completion,
from SimAI's trace and TwiNS's report.
All TwiNS timings are of `127ce90` (one run each, the machine otherwise idle).
- **Large runs** (128 GPUs, many entities): TwiNS is 10–14× faster than SimAI.
- **Small dense runs** (16 GPUs, NVLink saturated for tens of ms):
  - Only ~20 entities run each 25 ns slot, so the GPU is latency-bound. The slowest block of a
    slot, often a GPU host with ~150k cycles of dependent global accesses, sets the pace.
  - TwiNS goes from 1.09× slower (MoE on AlibabaHPN, whose 2 NICs per GPU make 3 link rates) to
    2.7× faster (DCN+, few enough blocks for persistent launches); it is faster than SimAI on 15
    of the 16 runs.
  - Batching recovers the throughput. The 9 AICB runs (2363520 messages) as one batch
    (`--batch`, 9 worlds, `--tail=25`) took 283 s at `0fbb333`, every message identical. One at
    a time, SimAI's runs take 791 s.
- **Since `ai-p3`** (these timings):
  - GPUs share the small switches' kernel (2 kernels per slot, not 3);
  - the streams are made so that the two kernels get different hardware queues;
  - persistent launches for small topologies;
  - `__launch_bounds__(32)`, and GPU hosts dispatched first.
  - The 128-GPU runs went 7.9–10.0 → 5.7–8.0 s.
- **Since `0fbb333`** (`0052771`), guided by thread traces (rocprofv3 ATT):
  - `Sim` is `__constant__` (`c_sim`). As a kernel argument whose address the contexts take,
    every wave copied it to scratch at its start and read every field from there.
  - The host context lives in LDS for one device too, and frames stay out of scratch (the INT
    hop is written by constant indices; an arrival's frame is read in place).
  - DCQCN: each device keeps a lower bound on its queue pairs' timers (`Host::due_min`), so a
    slot reads them only when one is due.
  - Windows (`--win`) are opt-in; in busy networks their checks cost more than they save.
  - The 128-GPU runs: 5.8–8.1 → 4.7–6.9 s (interleaved A/B, same machine state).
  - Tried without gain: power-of-two link rings (masks for `%`) and 32-bit arrival-slot division
    (HPCC +5%).
- **Since `0052771`** (`6eaf61f`, `04ef5aa`, `fa55288`):
  - The slot kernel was 1.57 MB of code, mostly inlined 64-bit division routines (the GPU has no
    integer division). Divisors fixed at setup (the slot, ring sizes) divide by a precomputed
    multiply-high (`FDiv`, exact for every 64-bit numerator), rates by `udiv64` (a double
    estimate, then exact integer steps), and queue heads step without division.
  - The cold DAG path (`dag_msg`, `dag_fire`, `memo_get`) is out of line; with the divisions gone,
    the kernel is 0.68 MB.
    Out-of-line calls on hot paths cost far more (spills around each call: HPCC +69%).
  - A block's entry loads its entity's static data (`BlockInfo`), the slot and the finish flag
    together, not as a chain of dependent loads.
  - Device members are always inlined: when clang stopped inlining `Arrivals::init`, the call put
    the frames back in scratch (HPCC +6%).
  - About 2–7% together. With `0052771`, the 16-GPU AICB runs went 31.9–122.0 s (at `0fbb333`) →
    26.4–95.2 s.
  - Tried without gain: persistent launches that include the wide switches (−2%), `-mcumode`
    (−2% on small runs, +1–2% at 128 GPUs).
- **Since `fa55288`** (`62f7ece`): the wide switches' blocks have a lane per port (up to 256, in
  whole waves), not 32. AlibabaHPN's 122- and 136-port switches had loaded, pooled and stored
  their ports in 4–5 rounds. AlibabaHPN 128 6.9 → 5.4 s, MoE on AlibabaHPN 69.3 → 62.1 s,
  Spectrum-X 128 4.8 → 4.4 s, Llama on AlibabaHPN 86.0 → 82.6 s, GPT-13B on AlibabaHPN
  95.2 → 87.1 s.
- **Since `62f7ece`** (`9a418c1`): each lane of a wide switch still walked the ports before its
  own to find its arrivals (~280 cycles a step), and the block waited at the next barrier for the
  lanes of its last ports. A block-wide exclusive scan (shuffles within each wave, then the waves'
  sums) gives every port's first arrival once, and an arrival finds its port by binary search.
  AlibabaHPN 128 5.4 → 5.0 s, Spectrum-X 128 4.4 → 4.2 s, MoE on AlibabaHPN 62.1 → 59.7 s,
  GPT-13B on AlibabaHPN 87.1 → 82.2 s. What remains on a wide switch is its serial event loop
  (lane 0).
- **Since `9a418c1`** (`127ce90`): a host's selection reads its candidates' times (per device:
  arrival, TransmitComplete, m_nextSend; the timer, flow start, DAG flow) in one unrolled pass,
  then finds the earliest; only candidates sharing it go through the comparisons, in the same
  order. Before, each candidate was a chain of LDS reads and branches in a loop over the device
  count (12 for AlibabaHPN's 3-device GPU hosts; selection was ~29% of the critical host wave).
  About −2 to −3.5% everywhere: MoE on AlibabaHPN 59.7 → 58.4 s, GPT-13B on AlibabaHPN
  82.2 → 81.2 s, Llama on AlibabaHPN 79.1 → 77.6 s.

## Bugs found on the way (TwiNS)

- **Bucket overflow.** A link that sends 60-byte frames in 0 ns (NVLink at 2880 Gb/s) has no
  bound on the frames that reach a bucket. Its bucket now holds `MAXF` frames; before, AlibabaHPN
  MoE overflowed it 5 times.
- **Ordinal shift after QpComplete.** After the callback, the ACK event's later events take
  ordinals after its actions (`h_ack`).
