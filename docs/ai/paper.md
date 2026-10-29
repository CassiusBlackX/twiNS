# TwiNS against SimAI on the SimAI paper's workloads

**Status.** In progress. Larger training runs (512 and 1024 GPUs) and the multi-threaded baselines
are still running on a87.

**Result so far.**
- 35 runs compared, from 128 to 1024 GPUs, of the workloads in SimAI's own paper (NSDI '25 §4).
- All 35 are identical to SimAI: every message's start and completion, to the nanosecond.
- That includes:
  - full-size training iterations: GPT-3 13B, LLaMA 65B (PP 2) and GPT-3 175B (PP 8) at 128
    GPUs, up to 5.97 M messages;
  - 1024-GPU AlibabaHPN, whose switches have 248 ports.
- Against SimAI on one core, TwiNS on an RX 9060 XT is:
  - 4–55× faster on collectives;
  - 6–13× faster on 128-GPU training;
  - 16–21× faster on 512-GPU training probes.

## Workloads

`validation/simai/gen_paper.sh` generates them; `run_paper.sh` runs them on a87.

**Topology.** AlibabaHPN, H100, 200 Gb/s, dual ToR, 8 GPUs per server. The paper's testbeds are
HPN-like multi-rail clusters.

**Collectives across hosts (§4.2).**
- AllReduce, AllGather and ReduceScatter of 8, 64 and 512 MB.
- Each runs in the data-parallel group with TP 8, so a GPU's group is the same GPU of every server
  (one rail).
- 128, 512 and 1024 GPUs.

**Training (§4.4, Table 3).** From AICB:
- GPT-3 13B: TP 2.
- LLaMA 65B: TP 8, PP 2.
- GPT-3 175B: TP 8, PP 8.

Each pipeline runs PP micro-batches of 1. Three sizes:
- **full**: 40, 80 and 96 layers;
- **mid**: 8, 8 and 16 layers;
- **probe**: 2 layers per stage.

**Full iterations at 512 GPUs and more take single-threaded SimAI over a day.** Their gradient
collectives grow with the layers: 26.5 GB for the 13B model's ReduceScatter. At 128 GPUs, a full
iteration simulates 0.56–0.90 s. So larger scales use the mid or probe sizes. They have the same
parallel groups and communication pattern, with fewer layers.

**SimAI runs.**
- `simai-trace`: sequential, instrumented as in `e2e.md`.
- `simai-seq`: sequential, untraced, timed alone. This is the speed baseline where available,
  otherwise the traced time.
- Every run is in a podman container with a memory cap; a87 relays the 5090 and must never run out.
- Memory peaks:
  - collectives: 5.7 GB at 128 GPUs, 7.4–8.0 GB at 512, 10.5–11.6 GB at 1024;
  - full training at 128 GPUs: 5.6–14.5 GB.

## Results

TwiNS at `00743b5` (global windows on), RX 9060 XT. "Speedup" is against the untraced time where
there is one, else the traced one.

| run | messages | identical | simulated (ms) | SimAI traced (s) | SimAI untraced (s) | SimAI peak (GB) | TwiNS (s) | speedup |
|---|---|---|---|---|---|---|---|---|
| run | messages | identical | simulated (ms) | SimAI traced (s) | SimAI untraced (s) | SimAI peak (GB) | TwiNS (s) | speedup |
|---|---|---|---|---|---|---|---|---|
| c_allgather_512m_hpn128 | 1920 | 1920 | 23.37 | 54.7 | 54.4 | — | 11.6 | 4.7× |
| c_allgather_64m_hpn128 | 1920 | 1920 | 2.63 | 10.9 | 10.2 | — | 1.3 | 7.6× |
| c_allgather_8m_hpn128 | 1920 | 1920 | 0.40 | 5.4 | 4.8 | — | 0.2 | 26.1× |
| c_allreduce_512m_hpn128 | 3840 | 3840 | 55.66 | 102.6 | 102.7 | — | 25.7 | 4.0× |
| c_allreduce_64m_hpn128 | 3840 | 3840 | 5.25 | 17.1 | 16.6 | — | 2.7 | 6.2× |
| c_allreduce_8m_hpn128 | 3840 | 3840 | 0.80 | 6.4 | 6.2 | — | 0.4 | 17.3× |
| c_reducescatter_512m_hpn128 | 1920 | 1920 | 23.37 | 54.0 | 55.2 | — | 11.4 | 4.8× |
| c_reducescatter_64m_hpn128 | 1920 | 1920 | 2.63 | 11.0 | 10.2 | — | 1.3 | 7.7× |
| c_reducescatter_8m_hpn128 | 1920 | 1920 | 0.40 | 5.5 | 4.8 | — | 0.2 | 26.3× |
| gpt3_13b_probe_hpn128 | 42240 | 42240 | 56.37 | 517.8 | — | — | 86.1 | 6.0× |
| gpt3_175b_probe_hpn128 | 1385344 | 1385344 | 189.25 | 2125.2 | — | 7.5 | 166.8 | 12.7× |
| llama_65b_probe_hpn128 | 439168 | 439168 | 81.35 | 620.4 | — | — | 60.4 | 10.3× |
| c_allgather_512m_hpn512 | 32256 | 32256 | 27.80 | 294.9 | 258.1 | 7.4 | 23.6 | 10.9× |
| c_allgather_64m_hpn512 | 32256 | 32256 | 3.01 | 44.6 | 39.4 | 7.4 | 2.7 | 14.4× |
| c_allgather_8m_hpn512 | 32256 | 32256 | 0.67 | 13.0 | 13.0 | 7.4 | 0.4 | 34.1× |
| c_allreduce_512m_hpn512 | 64512 | 64512 | 108.05 | 533.7 | 490.0 | 7.5 | 75.8 | 6.5× |
| c_allreduce_64m_hpn512 | 64512 | 64512 | 6.02 | 84.7 | 74.1 | 7.5 | 5.5 | 13.3× |
| c_allreduce_8m_hpn512 | 64512 | 64512 | 1.35 | 21.2 | 19.0 | 7.5 | 0.8 | 23.9× |
| c_reducescatter_512m_hpn512 | 32256 | 32256 | 27.75 | 268.5 | 250.8 | 7.4 | 23.7 | 10.6× |
| c_reducescatter_64m_hpn512 | 32256 | 32256 | 3.01 | 45.4 | 38.7 | 7.4 | 2.8 | 13.7× |
| c_reducescatter_8m_hpn512 | 32256 | 32256 | 0.67 | 14.5 | 12.9 | 7.4 | 0.4 | 34.0× |
| c_allgather_512m_hpn1024 | 130048 | 130048 | 29.95 | 738.0 | 542.8 | 10.6 | 30.7 | 17.7× |
| c_allgather_64m_hpn1024 | 130048 | 130048 | 3.37 | 133.7 | 92.4 | 10.6 | 3.6 | 25.7× |
| c_allgather_8m_hpn1024 | 130048 | 130048 | 1.02 | 44.1 | 34.2 | 10.5 | 0.6 | 54.5× |
| c_allreduce_512m_hpn1024 | 260096 | 260096 | 172.60 | 1163.3 | 1079.0 | 10.8 | 129.2 | 8.4× |
| c_allreduce_64m_hpn1024 | 260096 | 260096 | 6.75 | 247.9 | 171.6 | 10.8 | 7.3 | 23.6× |
| c_allreduce_8m_hpn1024 | 260096 | 260096 | 2.04 | 67.6 | 53.2 | 10.8 | 1.2 | 42.7× |
| c_reducescatter_512m_hpn1024 | 130048 | 130048 | 43.90 | 733.9 | 530.5 | 10.6 | 37.7 | 14.1× |
| c_reducescatter_64m_hpn1024 | 130048 | 130048 | 3.38 | 133.0 | 86.4 | 10.6 | 3.6 | 23.9× |
| c_reducescatter_8m_hpn1024 | 130048 | 130048 | 1.02 | 46.3 | 32.1 | 10.6 | 0.6 | 50.9× |
| gpt3_13b_full_hpn128 | 120064 | 120064 | 557.97 | 5220.5 | — | 5.6 | 824.3 | 6.3× |
| llama_65b_full_hpn128 | 4797312 | 4797312 | 904.12 | 6673.1 | — | 12.7 | 639.0 | 10.4× |
| gpt3_175b_full_hpn128 | 5972864 | 5972864 | 734.78 | 6384.6 | — | 14.5 | 566.2 | 11.3× |
| gpt3_13b_probe_hpn512 | 562176 | 562176 | 73.74 | 2986.5 | — | 8.9 | 189.6 | 15.8× |
| llama_65b_probe_hpn512 | 1781248 | 1781248 | 153.75 | 3526.2 | — | 10.1 | 170.1 | 20.7× |

## What sets the speedup

TwiNS advances in 25 ns slots, the NVLink delay. A launch costs a nearly fixed 30–50 µs, set by its
slowest entity. SimAI's cost grows with its events.
- **More GPUs and smaller messages mean more events per slot**, so a higher speedup. AllGather of
  8 MB is 26× at 128 GPUs and 55× at 1024.
- **Large messages are a few long flows in steady state.** Before global windows they reached only
  1.3–1.6× at 128 GPUs; now they reach 4–4.8×.
- **Global windows** (`--gwin`, default; `00743b5`). While no NVLink is busy, every link that can
  carry a frame takes at least 500 ns, so one launch runs up to 20 slots for every entity. The
  512 MB AllGather at 128 GPUs goes from 934 528 launches to 46 784: 33.8 → 11.6 s.
- **Training alternates.** TP traffic over NVLink needs single slots; DP gradient phases leave
  NVLink quiet and run in windows. Full GPT-3 13B (TP 2) runs 19.1 M launches over 22.3 M slots;
  LLaMA 65B runs 8.2 M over 36.2 M.

## Speed baselines still pending

**SimAI multi-threaded** (`simai-mtp`, Unison), `-t 16/32/64`: time, whether the run completes,
whether two runs agree. Phase 0 (`baseline.md`) found it irreproducible, at most 1.3× faster at 16
GPUs, and crashing or losing flows at 128 GPUs.

## TwiNS changes that made these runs possible or faster

- `fde54b6`: wide switches run safe slots, a lane per egress port. They meet the safe condition in
  100% of the slots measured.
- `315c9a1`: wide switches whose ports exceed LDS (184 ports at 512 GPUs, 248 at 1024) keep them in
  global memory; at most 256 ports.
- `00743b5`: global windows. Each NVSwitch files its server's NVLink horizon. An NVLink flow that
  starts during a launch forces the next launch to one slot: a race that `g_sx16_ring` and
  `d_sx16_ring` exposed, now fixed.

## Rejected, with measurements

- **Persistent launches for topologies with wide switches.** The GPU holds only 80–96 of these
  blocks at once (16 multiprocessors × 5–6), fewer than AlibabaHPN 16 (154) or Spectrum-X 16
  (90).
- **A multi-device host's devices on separate lanes.** It can be exact with some care: events go
  to the device whose state they change, and DAG starts need a host-level prologue. But a host
  slot has 2.1–3.6 events, and its busiest device holds 89–99% of them, so the bound is
  1.01–1.12×.
- **Per-entity windows (`--win`).** They gain about 10%: the NVSwitches keep one-slot windows, so
  the launches stay one per slot.
