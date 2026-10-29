# TwiNS against SimAI on the SimAI paper's workloads

**Status.** In progress. The timing columns are provisional until the untraced baselines (A4)
are in.

**Result so far.**
- 30 runs compared, from 128 to 1024 GPUs, of the workloads in SimAI's own paper (NSDI '25 §4).
- All 30 are identical to SimAI: every message's start and completion, to the nanosecond.
- That includes pipeline parallelism (LLaMA 65B with PP 2, GPT-3 175B with PP 8) and 1024-GPU
  AlibabaHPN with 248-port switches.
- TwiNS's speedup grows with scale and shrinks with message size, from 1.3× (512 MB AllReduce,
  128 GPUs) to 39× (8 MB ReduceScatter, 1024 GPUs).

## Workloads

`validation/simai/gen_paper.sh` generates them; `run_paper.sh` runs them on a87.

**Topology.** AlibabaHPN, H100, 200 Gb/s, dual ToR, 8 GPUs per server. The paper's testbeds are
HPN-like multi-rail clusters. The 1024-GPU topology has 248-port switches.

**Collectives across hosts (§4.2).**
- AllReduce, AllGather and ReduceScatter of 8, 64 and 512 MB.
- Each runs in the data-parallel group with TP 8, so a GPU's group is the same GPU of every server
  (one rail), as in the paper's multi-rail setup.
- 128, 512 and 1024 GPUs.

**Training (§4.4, Table 3).** From AICB:
- GPT-3 13B: TP 2.
- LLaMA 65B: TP 8, PP 2.
- GPT-3 175B: TP 8, PP 8.

Each pipeline runs PP micro-batches.
- **Probes**: 2 layers per stage, used to estimate cost.
- **Full layer counts**: 40, 80 and 96. These are running.
- With full layers, the gradient collectives are large: 26.5 GB for the 13B model's
  ReduceScatter. A full iteration therefore simulates far longer than a probe, and SimAI's cost
  cannot be extrapolated from the probes by layer count.

**SimAI runs.**
- `simai-trace`: SimAI sequential, instrumented as in `e2e.md`.
- Each run is in a container capped at 6–24 GB.
- Memory peaks:
  - collectives: 5.7 GB at 128 GPUs, 7.4–8.0 GB at 512, 10.5–11.6 GB at 1024;
  - GPT-3 175B probe: 8.0 GB.

## Results

"SimAI traced" is the instrumented run's wall time. Up to 12 ran side by side on a87's 64 cores.
TwiNS ran on an RX 9060 XT at `315c9a1`.

| run | messages | identical | simulated (ms) | SimAI traced (s) | SimAI peak (GB) | TwiNS (s) | ratio |
|---|---|---|---|---|---|---|---|
| c_allgather_512m_hpn128 | 1920 | 1920 | 23.37 | 54.7 | — | 33.5 | 1.6× |
| c_allgather_64m_hpn128 | 1920 | 1920 | 2.62 | 10.9 | — | 3.4 | 3.2× |
| c_allgather_8m_hpn128 | 1920 | 1920 | 0.40 | 5.4 | — | 0.5 | 11.8× |
| c_allreduce_512m_hpn128 | 3840 | 3840 | 55.66 | 102.6 | — | 77.3 | 1.3× |
| c_allreduce_64m_hpn128 | 3840 | 3840 | 5.25 | 17.1 | — | 7.0 | 2.4× |
| c_allreduce_8m_hpn128 | 3840 | 3840 | 0.80 | 6.4 | — | 0.9 | 6.7× |
| c_reducescatter_512m_hpn128 | 1920 | 1920 | 23.37 | 54.0 | — | 33.4 | 1.6× |
| c_reducescatter_64m_hpn128 | 1920 | 1920 | 2.63 | 11.0 | — | 3.5 | 3.2× |
| c_reducescatter_8m_hpn128 | 1920 | 1920 | 0.40 | 5.5 | — | 0.5 | 12.1× |
| gpt3_13b_probe_hpn128 | 42240 | 42240 | 56.37 | 517.8 | — | 88.5 | 5.9× |
| gpt3_175b_probe_hpn128 | 1385344 | 1385344 | 189.25 | 2125.2 | 7.5 | 285.8 | 7.4× |
| llama_65b_probe_hpn128 | 439168 | 439168 | 81.35 | 620.4 | — | 120.8 | 5.1× |
| c_allgather_512m_hpn512 | 32256 | 32256 | 27.80 | 294.9 | 7.4 | 47.1 | 6.3× |
| c_allgather_64m_hpn512 | 32256 | 32256 | 3.01 | 44.6 | 7.4 | 5.2 | 8.6× |
| c_allgather_8m_hpn512 | 32256 | 32256 | 0.67 | 13.0 | 7.4 | 0.8 | 15.7× |
| c_allreduce_512m_hpn512 | 64512 | 64512 | 108.05 | 533.7 | 7.5 | 166.0 | 3.2× |
| c_allreduce_64m_hpn512 | 64512 | 64512 | 6.02 | 84.7 | 7.5 | 10.5 | 8.1× |
| c_allreduce_8m_hpn512 | 64512 | 64512 | 1.35 | 21.2 | 7.5 | 1.7 | 12.6× |
| c_reducescatter_512m_hpn512 | 32256 | 32256 | 27.75 | 268.5 | 7.4 | 47.0 | 5.7× |
| c_reducescatter_64m_hpn512 | 32256 | 32256 | 3.01 | 45.4 | 7.4 | 5.4 | 8.4× |
| c_reducescatter_8m_hpn512 | 32256 | 32256 | 0.67 | 14.5 | 7.4 | 0.8 | 17.5× |
| c_allgather_512m_hpn1024 | 130048 | 130048 | 29.95 | 738.0 | 10.6 | 65.9 | 11.2× |
| c_allgather_64m_hpn1024 | 130048 | 130048 | 3.37 | 133.7 | 10.6 | 7.7 | 17.5× |
| c_allgather_8m_hpn1024 | 130048 | 130048 | 1.02 | 44.1 | 10.5 | 1.2 | 37.0× |
| c_allreduce_512m_hpn1024 | 260096 | 260096 | 172.60 | 1163.3 | 10.8 | 326.1 | 3.6× |
| c_allreduce_64m_hpn1024 | 260096 | 260096 | 6.75 | 247.9 | 10.8 | 15.6 | 15.9× |
| c_allreduce_8m_hpn1024 | 260096 | 260096 | 2.04 | 67.6 | 10.8 | 2.6 | 26.2× |
| c_reducescatter_512m_hpn1024 | 130048 | 130048 | 43.90 | 733.9 | 10.6 | 86.3 | 8.5× |
| c_reducescatter_64m_hpn1024 | 130048 | 130048 | 3.38 | 133.0 | 10.6 | 7.7 | 17.4× |
| c_reducescatter_8m_hpn1024 | 130048 | 130048 | 1.02 | 46.3 | 10.6 | 1.2 | 38.6× |

## Why the speedup varies

TwiNS advances in 25 ns slots, the NVLink delay. Each slot costs a nearly fixed ~32–50 µs, set by
its slowest entity. SimAI's cost is proportional to its events. The speedup is therefore the work
SimAI does per 25 ns divided by that fixed cost.
- **Small messages, many GPUs: high.** Many flows run at once, so each slot is busy.
- **Large messages: low.** The rail rings run a few long flows in steady state, with few events
  per slot. Example: 512 MB at 128 GPUs is 1.3–1.6×.
- **Windows do not help here.** Per-entity windows (`--win`) gain only about 10% on the 512 MB
  AllGather at 128 GPUs. The launches stay one per slot, because some entity is always busy.

**Next lever.** When no NVLink traffic is in flight, every link carrying frames has a delay of
500 ns or more. A launch could then advance all entities by many slots at once, the global
lookahead.

## Speed baselines (pending)

- **SimAI sequential, untraced** (`simai-seq`): each run timed alone.
- **SimAI multi-threaded** (`simai-mtp`, Unison) with `-t 16/32/64`: time, whether the run
  completes, and whether two runs agree. Phase 0 (`baseline.md`) found it irreproducible, at most
  1.3× faster at 16 GPUs, and crashing or losing flows at 128 GPUs.

## TwiNS changes that made these runs possible

- `fde54b6`: wide switches run safe slots, a lane per egress port. They meet the safe condition in
  100% of the slots measured.
- `315c9a1`: wide switches whose ports exceed LDS (184 ports at 512 GPUs, 248 at 1024) keep them in
  global memory; at most 256 ports.

## Rejected, with measurements

- **Persistent launches for topologies with wide switches.** The GPU holds only 80–96 of these
  blocks at once (16 multiprocessors × 5–6), fewer than AlibabaHPN 16 (154) or Spectrum-X 16
  (90).
- **A multi-device host's devices on separate lanes.** It can be exact with some care: events go
  to the device whose state they change, and DAG starts need a host-level prologue. But a host
  slot has 2.1–3.6 events, and its busiest device holds 89–99% of them, so the bound is
  1.01–1.12×.
