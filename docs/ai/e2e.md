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
| tr_sx8 | 8 | Spectrum-X | microAllReduce | 1792 | 1792 | yes | 2.0 | 1.0 |
| tr_sx16 | 16 | Spectrum-X | microAllReduce | 7680 | 7680 | yes | 3.6 | 2.0 |
| tr_hpn16 | 16 | AlibabaHPN | microAllReduce | 7680 | 7680 | yes | 5.4 | 3.4 |
| tr_dcn16 | 16 | DCN+ | microAllReduce | 7680 | 7680 | yes | 1.8 | 1.9 |
| tr_sx128 | 128 | Spectrum-X | microAllReduce | 520192 | 520192 | yes | 60.4 | 7.9 |
| tr_hpn128 | 128 | AlibabaHPN | microAllReduce | 520192 | 520192 | yes | 62.7 | 9.8 |
| tr_dcn128 | 128 | DCN+ | microAllReduce | 520192 | 520192 | yes | 52.6 | 10.0 |
| a_llama_sx16 | 16 | Spectrum-X | Megatron Llama-7B-like, tp 8 | 517008 | 517008 | yes | 121.4 | 100.2 |
| a_llama_hpn16 | 16 | AlibabaHPN | Megatron Llama-7B-like, tp 8 | 517008 | 517008 | yes | 122.4 | 126.5 |
| a_llama_dcn16 | 16 | DCN+ | Megatron Llama-7B-like, tp 8 | 517008 | 517008 | yes | 120.3 | 84.7 |
| a_gpt13_sx16 | 16 | Spectrum-X | Megatron GPT-13B-like, tp 8, SP | 264336 | 264336 | yes | 95.3 | 95.8 |
| a_gpt13_hpn16 | 16 | AlibabaHPN | Megatron GPT-13B-like, tp 8, SP | 264336 | 264336 | yes | 98.0 | 138.6 |
| a_gpt13_dcn16 | 16 | DCN+ | Megatron GPT-13B-like, tp 8, SP | 264336 | 264336 | yes | 93.2 | 76.6 |
| a_moe_sx16 | 16 | Spectrum-X | MoE, tp 2, ep 8 | 6496 | 6496 | yes | 49.6 | 66.8 |
| a_moe_hpn16 | 16 | AlibabaHPN | MoE, tp 2, ep 8 | 6496 | 6496 | yes | 53.8 | 118.7 |
| a_moe_dcn16 | 16 | DCN+ | MoE, tp 2, ep 8 | 6496 | 6496 | yes | 37.2 | 41.7 |

SimAI is the single-threaded sequential build on a87 (Xeon Gold 5218; up to 9 runs side by side).
TwiNS runs on an RX 9060 XT. "Last event" is the run's end: the last timer firing or completion,
from SimAI's trace and TwiNS's report.
- **Large runs** (128 GPUs, many entities): TwiNS is 5–8× faster than SimAI.
- **Small dense runs** (16 GPUs, NVLink saturated for tens of ms):
  - Only ~20 entities run each 25 ns slot, so the GPU is latency-bound.
  - TwiNS is about as fast as SimAI: from 2.2× slower (MoE on AlibabaHPN, whose 2 NICs per GPU
    make 3 link rates) to 1.4× faster.
  - Batching recovers the throughput. The 9 AICB runs (2363520 messages) as one batch
    (`--batch`, 9 worlds) take 272 s, every message identical. Run one at a time, they take
    852 s in TwiNS and 791 s in SimAI. (The batch decided one tie on an overwritten chain tail,
    which changed nothing; a batch this size wants `--tail=25`.)
  - Server entities (an NVSwitch with its GPUs in one block, with a 500 ns slot) would address
    this.

## Bugs found on the way (TwiNS)

- **Bucket overflow.** A link that sends 60-byte frames in 0 ns (NVLink at 2880 Gb/s) has no
  bound on the frames that reach a bucket. Its bucket now holds `MAXF` frames; before, AlibabaHPN
  MoE overflowed it 5 times.
- **Ordinal shift after QpComplete.** After the callback, the ACK event's later events take
  ordinals after its actions (`h_ack`).
