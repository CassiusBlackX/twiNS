# TwiNS against SimAI on the SimAI paper's workloads

These runs replay SimAI's recordings. `whatif.md` runs the same workloads with no recording.

**Result.**
- 42 runs compared, from 128 to 1024 GPUs, of the workloads in SimAI's own paper (NSDI '25 §4).
- All 42 are identical to SimAI: every message's start and completion, to the nanosecond.
- Past 2^32 events, the reference is SimAI with 64-bit event uids. SimAI's own 32-bit uids wrap
  there and reorder simultaneous events; see below.
- That includes:
  - full-size training iterations: GPT-3 13B, LLaMA 65B (PP 2) and GPT-3 175B (PP 8) at 128
    GPUs, up to 5.97 M messages;
  - training at 512 and 1024 GPUs (mid and probe sizes), up to 11.2 M messages (GPT-3 175B probe
    at 1024 GPUs);
  - 1024-GPU AlibabaHPN, whose switches have 248 ports.
- Against SimAI on one core, TwiNS on an RX 9060 XT is:
  - 11–122× faster on collectives;
  - 6–16× faster on 128-GPU training;
  - 15–44× faster on 512- and 1024-GPU training.
- SimAI's single-threaded runs take up to 6.5 h (GPT-3 175B probe at 1024 GPUs); TwiNS's take
  10 min.
- On an RTX 5090 host, closed loop with start-up: 11–49× on collectives, 32–48× on 128-GPU
  training, 30–72× on 512- and 1024-GPU training, all identical; see "On an RTX 5090".
- These times are the simulation loop, without start-up (about 0.5 s at 128 GPUs, 1.4 s at 1024), which
  matters only for the smallest runs.

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

TwiNS at `9829a00`, RX 9060 XT; GPT-3 175B probe at 1024 GPUs with `--dcap=8192`, which it needs to fit
(`whatif.md`, "Limits"). "Speedup" is against the untraced time where there is one, else the traced one.

| run | messages | identical | simulated (ms) | SimAI traced (s) | SimAI untraced (s) | SimAI peak (GB) | TwiNS (s) | speedup |
|---|---|---|---|---|---|---|---|---|
| c_allgather_512m_hpn128 | 1920 | 1920 | 23.37 | 54.7 | 54.4 | — | 4.2 | 13.0× |
| c_allgather_64m_hpn128 | 1920 | 1920 | 2.63 | 10.9 | 10.2 | — | 0.48 | 21.3× |
| c_allgather_8m_hpn128 | 1920 | 1920 | 0.40 | 5.4 | 4.8 | — | 0.08 | 62.3× |
| c_allreduce_512m_hpn128 | 3840 | 3840 | 55.66 | 102.6 | 102.7 | — | 9.2 | 11.1× |
| c_allreduce_64m_hpn128 | 3840 | 3840 | 5.25 | 17.1 | 16.6 | — | 0.94 | 17.7× |
| c_allreduce_8m_hpn128 | 3840 | 3840 | 0.80 | 6.4 | 6.2 | — | 0.14 | 44.6× |
| c_reducescatter_512m_hpn128 | 1920 | 1920 | 23.37 | 54.0 | 55.2 | — | 4.1 | 13.5× |
| c_reducescatter_64m_hpn128 | 1920 | 1920 | 2.63 | 11.0 | 10.2 | — | 0.48 | 21.1× |
| c_reducescatter_8m_hpn128 | 1920 | 1920 | 0.40 | 5.5 | 4.8 | — | 0.08 | 61.5× |
| gpt3_13b_probe_hpn128 | 42240 | 42240 | 56.37 | 517.8 | — | — | 86.4 | 6.0× |
| gpt3_175b_probe_hpn128 | 1385344 | 1385344 | 189.25 | 2125.2 | — | 7.5 | 130.0 | 16.3× |
| llama_65b_probe_hpn128 | 439168 | 439168 | 81.35 | 620.4 | — | — | 41.3 | 15.0× |
| c_allgather_512m_hpn512 | 32256 | 32256 | 27.80 | 294.9 | 258.1 | 7.4 | 6.8 | 38.0× |
| c_allgather_64m_hpn512 | 32256 | 32256 | 3.01 | 44.6 | 39.4 | 7.4 | 0.83 | 47.5× |
| c_allgather_8m_hpn512 | 32256 | 32256 | 0.67 | 13.0 | 13.0 | 7.4 | 0.14 | 93.5× |
| c_allreduce_512m_hpn512 | 64512 | 64512 | 108.05 | 533.7 | 490.0 | 7.5 | 20.4 | 24.0× |
| c_allreduce_64m_hpn512 | 64512 | 64512 | 6.02 | 84.7 | 74.1 | 7.5 | 1.6 | 45.5× |
| c_allreduce_8m_hpn512 | 64512 | 64512 | 1.35 | 21.2 | 19.0 | 7.5 | 0.27 | 71.7× |
| c_reducescatter_512m_hpn512 | 32256 | 32256 | 27.75 | 268.5 | 250.8 | 7.4 | 6.9 | 36.5× |
| c_reducescatter_64m_hpn512 | 32256 | 32256 | 3.01 | 45.4 | 38.7 | 7.4 | 0.83 | 46.8× |
| c_reducescatter_8m_hpn512 | 32256 | 32256 | 0.67 | 14.5 | 12.9 | 7.4 | 0.14 | 90.8× |
| c_allgather_512m_hpn1024 | 130048 | 130048 | 29.95 | 738.0 | 542.8 | 10.6 | 11.6 | 46.7× |
| c_allgather_64m_hpn1024 | 130048 | 130048 | 3.37 | 133.7 | 92.4 | 10.6 | 1.5 | 62.9× |
| c_allgather_8m_hpn1024 | 130048 | 130048 | 1.02 | 44.1 | 34.2 | 10.5 | 0.28 | 122.1× |
| c_allreduce_512m_hpn1024 | 260096 | 260096 | 172.60 | 1163.3 | 1079.0 | 10.8 | 39.9 | 27.0× |
| c_allreduce_64m_hpn1024 | 260096 | 260096 | 6.75 | 247.9 | 171.6 | 10.8 | 2.9 | 58.8× |
| c_allreduce_8m_hpn1024 | 260096 | 260096 | 2.04 | 67.6 | 53.2 | 10.8 | 0.55 | 97.3× |
| c_reducescatter_512m_hpn1024 | 130048 | 130048 | 43.90 | 733.9 | 530.5 | 10.6 | 13.2 | 40.1× |
| c_reducescatter_64m_hpn1024 | 130048 | 130048 | 3.38 | 133.0 | 86.4 | 10.6 | 1.5 | 58.5× |
| c_reducescatter_8m_hpn1024 | 130048 | 130048 | 1.02 | 46.3 | 32.1 | 10.6 | 0.28 | 113.8× |
| gpt3_13b_full_hpn128 | 120064 | 120064 | 557.97 | 5220.5 | — | 5.6 | 822.0 | 6.4× |
| llama_65b_full_hpn128 | 4797312 | 4797312 | 904.12 | 6673.1 | — | 12.7 | 425.6 | 15.7× |
| gpt3_175b_full_hpn128 | 5972864 | 5972864 | 734.78 | 6384.6 | — | 14.5 | 404.2 | 15.8× |
| gpt3_13b_probe_hpn512 | 562176 | 562176 | 73.74 | 2986.5 | — | 8.9 | 194.1 | 15.4× |
| llama_65b_probe_hpn512 | 1781248 | 1781248 | 153.75 | 3526.2 | — | 10.1 | 103.9 | 33.9× |
| llama_65b_mid_hpn512 | 2698752 | 2698752 | 244.28 | 5380.8 | — | 11.5 | 155.9 | 34.5× |
| gpt3_13b_probe_hpn1024 | 2172928 | 2172928 | 80.32 | 6139.4 | — | 16.5 | 335.5 | 18.3× |
| gpt3_13b_mid_hpn1024 | 2271232 | 2271232 | 184.44 | 16032.2 | — | 16.8 | 738.8 | 21.7× |
| llama_65b_mid_hpn1024 | 5463040 | 5463040 | 338.37 | 12517.3 | — | 18.8 | 284.8 | 44.0× |
| gpt3_175b_probe_hpn1024 | 11197440 | 11197440 | 527.91 | 23325.9 | — | 27.7 | 614.5 | 38.0× |
| gpt3_13b_mid_hpn512 | 611328 | 611328 | 175.93 | 7156.8 | — | 9.1 | 477.1 | 15.0× |
| llama_65b_probe_hpn1024 | 3628032 | 3628032 | 183.12 | 7026.8 | — | 16.0 | 186.1 | 37.8× |

## What sets the speedup

TwiNS advances in 25 ns slots, the NVLink delay. A launch's cost is set by its slowest entity. SimAI's
cost grows with its events. `speed.md` has the measurements.
- **More GPUs and smaller messages mean more events per slot**, so a higher speedup. AllGather of
  8 MB is 62× at 128 GPUs and 122× at 1024.
- **Large messages are a few long flows in steady state.** Before global windows they reached only
  1.3–1.6× at 128 GPUs; now they reach 11–14×.
- **Global windows** (`--gwin`, default; `00743b5`). While no NVLink is busy, every link that can
  carry a frame takes at least 500 ns, so one launch runs up to 20 slots for every entity. The
  512 MB AllGather at 128 GPUs goes from 934 528 launches to 46 784: 33.8 → 11.6 s. Since `6c2763b` an
  entity runs only the slots with something in them: 4.2 s.
- **Training alternates.** TP traffic over NVLink needs single slots; DP gradient phases leave
  NVLink quiet and run in windows. Full GPT-3 13B (TP 2) runs 19.1 M launches over 22.3 M slots;
  LLaMA 65B runs 8.2 M over 36.2 M. So GPT-3 13B stays at 6–22×, as it was before `6c2763b`.

## SimAI multi-threaded

`simai-mtp` is SimAI with Unison (`--enable-mtp`), optimized, at `-t 16/32/64`. It was run on the
same collectives, one run at a time.

| run | sequential (s) | `-t 16` (s) | `-t 32` (s) | `-t 64` (s) | messages that differ from sequential | TwiNS (s) |
|---|---|---|---|---|---|---|
| c_allreduce_64m_hpn128 | 16.5 | 9.0 | 7.8 | 11.9 | 53 of 3840 | 0.94 |
| c_allgather_512m_hpn128 | 54.4 | 20.2 | 18.9 | 32.4 | 298 of 1920 | 4.2 |
| c_allreduce_64m_hpn512 | 74.0 | 27.6 | 28.6 | 52.0 | 25 of 64512 | 1.6 |
| c_allgather_512m_hpn512 | 258.0 | 70.9 | 60.1, aborted: 31719 of 32256 done | 115.5 | 756 of 32256 | 6.8 |
| c_allreduce_64m_hpn1024 | 171.6 | 68.1 | 84.7 | 184.4 | 739 of 260096 | 2.9 |
| c_allgather_512m_hpn1024 | 542.7 | 144.7 | 133.4 | 228.0 | 2467 of 130048 | 11.6 |

- **Speed.** Threads gain 1.8–4.1× at `-t 16/32`; `-t 64` is slower. TwiNS is 4.5–23×
  faster than SimAI's best thread count.
- **Results differ from sequential SimAI.** The same messages differ at every thread count, so
  this is Unison's event order (see `phase0-notes.md`), not noise. One run aborted and lost 537
  messages.
- **So sequential SimAI is the reference.** Its untraced runs (`simai-seq`) are identical to the
  traced ones that TwiNS matches.

## How fair is "SimAI on one core"?

Measured 2026-11-04/05 on a87 (2 × Xeon Gold 5218, 32 cores, 123 GB), against TwiNS on this machine
(i5-7500, 4 cores; RX 9060 XT), final ai-nvl build, closed loop (no recording), best of the GPU alone
and `--nvl-islands=4`. a87's scripts: `fair.sh`, `fair_b.sh` in `~/netsim/aiwork`.

**Which SimAI TwiNS equals.** `simai-ref`: sequential SimAI with `simai.patch`, four fixes (a crash
when a port resumes after PFC, ECN draws from one stream per port, routes ordered by node id instead
of heap address, HPCC's INT header size), plus 64-bit event uids past 2^32 events. SimAI as shipped
(`simai-seq` here) differs from it: on GPT-3 13B probe at 128 GPUs in 75% of messages and 20% in
iteration time (45.2 against 56.4 ms); on LLaMA 65B and GPT-3 175B probes at 128 GPUs not at all. It
also crashes (SIGSEGV) on HPCC, on DCN+ with 4 MB buffers and on all-to-all over DCN+.

**SimAI's own times vary by 10–20%.** The same run, untraced:

| run | traced, earlier | untraced, 3–6 runs at once | untraced, alone (simai-ref / simai-seq) |
|---|---|---|---|
| GPT-3 13B probe, 128 GPUs | 518 s | 616 s | 497 / 510 s |
| LLaMA 65B probe, 512 GPUs | 3526 s | 3572 s | 3726 / 3290 s |

Tracing costs about 4%; other runs on the machine cost more (up to 46% on the largest). The speedups
below use SimAI's fastest time of all its runs.

**1. Against sequential SimAI, the exact reference** (42 paper runs):

| runs | speedup |
|---|---|
| collectives, 128 / 512 / 1024 GPUs | 8.5–16× / 9.5–26× / 6.8–29× |
| training, 128 GPUs | 11.7–14.7× |
| training, 512 / 1024 GPUs | 17.6–23.7× / 17.6–28.8× |

**2. Against SimAI with threads** (`simai-mtp`, Unison, `-t 16` and `-t 32`, best of the two, one run
at a time). Threads gain 3.2–6.0× on training and 1.8–4.1× on collectives, but the results are not
sequential SimAI's. Against the sequential build of the same code (unpatched):

| run | SimAI, threads | TwiNS | TwiNS faster | messages differing | iteration time |
|---|---|---|---|---|---|
| GPT-3 13B probe, 128 GPUs | 153 s | 42.4 s | 3.6× | 75% | +18.0% |
| LLaMA 65B probe, 128 GPUs | 195 s | 49.9 s | 3.9× | 0.3% | +0.009% |
| GPT-3 175B probe, 128 GPUs | 623 s | 146 s | 4.3× | 0.15% | +0.004% |
| GPT-3 13B probe, 512 GPUs | 501 s | 156 s | 3.2× | 93% | +2.2% |
| LLaMA 65B probe, 512 GPUs | 786 s | 154 s | 5.1× | 1.0% | +4.3% |
| GPT-3 13B probe, 1024 GPUs | 1102 s | 349 s | 3.2× | 96% | −9.9% |
| collectives (6, see above) | 7.8–133 s | 1.3–18.9 s | 3.6–7.7× | 0.04–16% | |

So TwiNS, exact, is 3.2–7.7× faster than SimAI at its fastest, which is not exact (iteration times
off by up to 18%).

**3. Throughput: a whole server of SimAI runs against one GPU.** Runs are independent, so a sweep can
run many SimAI processes at once, as many as memory allows.
- The 47 what-if runs (`whatif.md`), simai-ref, all on a87 within 100 GB of container caps: **794 s**
  from first start to last end; the longest single run (LLaMA probe with HPCC) took 769 s of it.
- TwiNS, the same 47 runs, all identical to SimAI: 1112 s one at a time with 4 islands, 1179 s on the
  GPU alone one at a time, 978 s two at a time, 1156 s three at a time, **854 s two at a time with 2
  islands each**.
- So on a sweep of small runs (128 GPUs and less), a 32-core server of SimAI matches or beats one
  consumer GPU (794 against 854 s): SimAI's runs fit side by side, and the batch takes as long as its
  longest run.
- On large runs it does not. A run cannot be split, and memory limits how many fit (16–28 GB each at
  1024 GPUs). The 15 training runs above took a87 9.4 h, the time of its longest run (GPT-3 175B probe
  at 1024 GPUs, 6.5 h on a quiet machine); TwiNS runs all 15 one after another in 1.4 h.

## On an RTX 5090

The 5090 host (RTX 5090, Ryzen 9950X with 16 cores), final ai-nvl build, 2026-11-07, the GPU otherwise
idle (another user's CPU jobs took about 1.5 cores at times). Closed loop, wall clock with start-up,
one run each; "RX" is this machine (i5-7500, RX 9060 XT), the same build (`whatif.md`, "Speed", and
`--nvl-islands=4`). SimAI is its fastest single-core time, as above. Raw results, outside the
repository: `../twiNS/out/5090/r1107` (`run5090.sh`; table by `out/tmp/ai_tab5090.py` there).

- **Exact.** All 42 paper runs identical to SimAI per message and per DAG line, on the GPU alone and
  with 16 islands; all 47 what-if runs too, both ways, with no undecided ties. Replay (`twins-rdma` on
  SimAI's recordings): 42 of 42 identical, 1.1–2.9× faster than on the RX host on collectives and
  1.5–3.5× on training (GPT-3 175B probe at 1024 GPUs: 183 s against 632 s).
- **Against sequential SimAI** (best of the GPU alone and 16 islands): collectives 15–17× at 128
  GPUs, 13–39× at 512, 11–49× at 1024; training 32–48× at 128 GPUs, 30–56× at 512, 39–72× at 1024.
  The RX host gives 8.5–16×, 9.5–26×, 6.8–29×; 11.7–14.7×, 17.6–23.7×, 17.6–28.8×.
- **Against SimAI with threads** (the runs in "How fair", 2.): 6.9–14× on training, 5.9–12× on the
  collectives (RX: 3.2–5.1×, 3.6–7.7×). Still exact, where the threads are not.
- **Throughput.** The 47 what-if runs: 849 s one at a time on the GPU alone, 620 s one at a time with
  16 islands, 827 s two at a time, 523 s two at a time with 8 islands each, **502 s four at a time with
  4 islands each** (all identical to SimAI), against a87's 794 s for 47 SimAI processes at once. The
  15 training runs, one after another: 0.57 h (RX: 1.4 h; a87, all at once: 9.4 h).
- **Where the time goes.** Islands pay where NVLink dominates (GPT-3 13B: 3.6–4.3× over the GPU
  alone at 128 GPUs) and cost at 1024 GPUs on LLaMA and GPT-3 175B (0.84–0.85×), where the system
  layer and the syncs, on one core, are most of what is left. Collectives gain 1.0–1.9× over the RX
  host, the shortest runs least (start-up); training 2.2–3.3×.

| run | SimAI, 1 core, fastest (s) | RX: GPU (s) | RX: 4 islands (s) | 5090: GPU (s) | 5090: 16 islands (s) | 5090 best vs RX best | RX vs SimAI | 5090 vs SimAI |
|---|---|---|---|---|---|---|---|---|
| c_allgather_512m_hpn128 | 54.4 | 5.50 | 5.30 | 3.20 | 3.20 | 1.66× | 10.3× | 17.0× |
| c_allgather_64m_hpn128 | 10.2 | 1.10 | 0.80 | 0.60 | 0.60 | 1.33× | 12.7× | 17.0× |
| c_allgather_8m_hpn128 | 4.8 | 0.70 | 0.30 | 0.30 | 0.30 | 1.00× | 16.0× | 16.0× |
| c_allreduce_512m_hpn128 | 102.6 | 12.20 | 12.10 | 6.90 | 6.80 | 1.78× | 8.5× | 15.1× |
| c_allreduce_64m_hpn128 | 16.6 | 1.70 | 1.30 | 1.00 | 1.00 | 1.30× | 12.8× | 16.6× |
| c_allreduce_8m_hpn128 | 6.2 | 0.80 | 0.40 | 0.40 | 0.40 | 1.00× | 15.5× | 15.5× |
| c_reducescatter_512m_hpn128 | 54.0 | 5.50 | 5.20 | 3.10 | 3.10 | 1.68× | 10.4× | 17.4× |
| c_reducescatter_64m_hpn128 | 10.2 | 1.20 | 0.80 | 0.70 | 0.60 | 1.33× | 12.7× | 17.0× |
| c_reducescatter_8m_hpn128 | 4.8 | 0.70 | 0.30 | 0.40 | 0.30 | 1.00× | 16.0× | 16.0× |
| gpt3_13b_probe_hpn128 | 497.1 | 89.90 | 42.40 | 55.90 | 15.50 | 2.74× | 11.7× | 32.1× |
| gpt3_175b_probe_hpn128 | 2125.2 | 154.20 | 145.90 | 93.10 | 44.60 | 3.27× | 14.6× | 47.7× |
| llama_65b_probe_hpn128 | 620.4 | 49.90 | 50.70 | 29.90 | 17.60 | 2.84× | 12.4× | 35.2× |
| c_allgather_512m_hpn512 | 258.1 | 10.40 | 10.00 | 6.80 | 6.70 | 1.49× | 25.8× | 38.5× |
| c_allgather_64m_hpn512 | 39.4 | 2.60 | 2.00 | 1.70 | 1.60 | 1.25× | 19.7× | 24.6× |
| c_allgather_8m_hpn512 | 13.0 | 1.70 | 1.10 | 1.00 | 0.90 | 1.22× | 11.8× | 14.4× |
| c_allreduce_512m_hpn512 | 490.0 | 35.20 | 35.10 | 20.30 | 20.20 | 1.74× | 14.0× | 24.3× |
| c_allreduce_64m_hpn512 | 74.1 | 4.20 | 3.60 | 2.90 | 2.70 | 1.33× | 20.6× | 27.4× |
| c_allreduce_8m_hpn512 | 19.0 | 2.50 | 2.00 | 1.50 | 1.50 | 1.33× | 9.5× | 12.7× |
| c_reducescatter_512m_hpn512 | 250.8 | 10.60 | 10.20 | 6.90 | 6.70 | 1.52× | 24.6× | 37.4× |
| c_reducescatter_64m_hpn512 | 38.7 | 2.60 | 2.10 | 1.70 | 1.70 | 1.24× | 18.4× | 22.8× |
| c_reducescatter_8m_hpn512 | 12.9 | 1.70 | 1.20 | 1.00 | 0.90 | 1.33× | 10.8× | 14.3× |
| c_allgather_512m_hpn1024 | 542.8 | 19.60 | 18.90 | 11.30 | 11.00 | 1.72× | 28.7× | 49.3× |
| c_allgather_64m_hpn1024 | 92.4 | 6.20 | 5.30 | 3.80 | 3.70 | 1.43× | 17.4× | 25.0× |
| c_allgather_8m_hpn1024 | 34.2 | 4.80 | 4.10 | 2.70 | 2.60 | 1.58× | 8.3× | 13.2× |
| c_allreduce_512m_hpn1024 | 1079.0 | 72.10 | 72.20 | 39.40 | 38.70 | 1.86× | 15.0× | 27.9× |
| c_allreduce_64m_hpn1024 | 171.6 | 11.30 | 10.40 | 6.80 | 6.50 | 1.60× | 16.5× | 26.4× |
| c_allreduce_8m_hpn1024 | 53.2 | 8.70 | 7.80 | 5.10 | 4.80 | 1.62× | 6.8× | 11.1× |
| c_reducescatter_512m_hpn1024 | 530.5 | 22.30 | 21.80 | 13.10 | 12.90 | 1.69× | 24.3× | 41.1× |
| c_reducescatter_64m_hpn1024 | 86.4 | 6.40 | 5.50 | 4.00 | 3.80 | 1.45× | 15.7× | 22.7× |
| c_reducescatter_8m_hpn1024 | 32.1 | 5.20 | 4.30 | 2.90 | 2.80 | 1.54× | 7.5× | 11.5× |
| gpt3_13b_full_hpn128 | 5220.5 | 835.30 | 354.60 | 530.30 | 123.90 | 2.86× | 14.7× | 42.1× |
| llama_65b_full_hpn128 | 6520.9 | 503.10 | 492.10 | 308.10 | 178.50 | 2.76× | 13.3× | 36.5× |
| gpt3_175b_full_hpn128 | 6384.6 | 499.90 | 493.00 | 307.30 | 172.90 | 2.85× | 13.0× | 36.9× |
| gpt3_13b_probe_hpn512 | 2986.5 | 214.20 | 156.30 | 125.20 | 63.80 | 2.45× | 19.1× | 46.8× |
| llama_65b_probe_hpn512 | 3289.7 | 153.70 | 197.30 | 115.90 | 111.60 | 1.38× | 21.4× | 29.5× |
| llama_65b_mid_hpn512 | 5380.8 | 227.30 | 287.80 | 105.60 | 96.30 | 2.36× | 23.7× | 55.9× |
| gpt3_13b_probe_hpn1024 | 6139.4 | 409.40 | 349.30 | 216.50 | 158.80 | 2.20× | 17.6× | 38.7× |
| gpt3_13b_mid_hpn1024 | 16032.2 | 823.00 | 702.10 | 411.70 | 288.30 | 2.44× | 22.8× | 55.6× |
| llama_65b_mid_hpn1024 | 12517.3 | 434.60 | 593.60 | 173.20 | 203.50 | 2.51× | 28.8× | 72.3× |
| gpt3_175b_probe_hpn1024 | 23325.9 | 887.40 | 1377.10 | 328.20 | 389.80 | 2.70× | 26.3× | 71.1× |
| gpt3_13b_mid_hpn512 | 6638.1 | 504.00 | 376.50 | 289.10 | 158.90 | 2.37× | 17.6× | 41.8× |
| llama_65b_probe_hpn1024 | 7026.8 | 279.50 | 402.50 | 108.80 | 128.60 | 2.57× | 25.1× | 64.6× |

Times are as `check_closed.sh` prints them (0.1 s), so the shortest collectives are rough.

## SimAI's event uids wrap after 2^32 events

The GPT-3 13B probe at 1024 GPUs first differed from SimAI.
- **Where.** The first difference was at 44.597 ms on GPU 39. An ACK's TransmitComplete on one NIC
  and a data frame's arrival on the other fell in the same nanosecond. SimAI ran the
  TransmitComplete first; TwiNS ran the arrival first, as its causal chains say (the arrival was
  scheduled 860 ns earlier).
- **Not TwiNS's windows or safe slots.** TwiNS gave the same result with global windows off and
  with every switch serial.
- **The cause.** ns-3.36 keeps event uids in 32 bits (`Scheduler::EventKey::m_uid`,
  `DefaultSimulatorImpl::m_uid`), and runs simultaneous events in uid order. The run passes 2^32
  events around 44.6 ms. The counter wraps, so the TransmitComplete, scheduled after the wrap, got
  the smaller uid.
- **Confirmed.**
  - `simai-dag-ref`, our ns-3 replay, which counts uids as SimAI does, flips the same pair.
  - SimAI rebuilt with 64-bit uids (`trace_patch.py`, `TWINS_UID64`) matches TwiNS exactly: all
    2 172 928 messages.
- **What we do.** The traced references now use 64-bit uids, like the other reproducibility
  fixes (node-id maps, per-port ECN streams).
  - Below 2^32 events this changes nothing.
  - Replay cannot tell the two apart when the wrap only reorders ranks that do not interact: GPT-3
    175B full at 128 GPUs also passes 2^32 and first kept its 32-bit reference, identical per
    message. `twins-ai`, which makes its own actions, showed the difference (`whatif.md`). Its
    reference is 64-bit now too, and identical.
  - Emulating the wrap in TwiNS would need a global count of ns-3's events and their global order
    across the slot of the wrap.

## TwiNS changes that made these runs possible or faster

- `fde54b6`: wide switches run safe slots, a lane per egress port. They meet the safe condition in
  100% of the slots measured.
- `315c9a1`: wide switches whose ports exceed LDS (184 ports at 512 GPUs, 248 at 1024) keep them in
  global memory; at most 256 ports.
- `00743b5`: global windows. Each NVSwitch files its server's NVLink horizon. An NVLink flow that
  starts during a launch forces the next launch to one slot: a race that `g_sx16_ring` and
  `d_sx16_ring` exposed, now fixed.
- `6c2763b`: a global window runs as one step for each entity.
- `056db5b`: a switch marked busy with nothing to come takes a step in its window.

## Two TwiNS fixes from the large runs

- **DAG messages per slot.** A slot held at most 4096 DAG messages. LLaMA 65B (PP 2) and the GPT-3
  175B probe at 1024 GPUs fire more in one slot. The extra messages were dropped (reported, rc 3),
  their flows never started, and the run went on through empty slots up to its stop time.
  - `--dcap` now sets the room (default 16384 since `75d050b`, 4096 before). On an overflow, the run
    starts again with 4×.
  - A quiescent run (nothing ran, nothing is due, nothing is on its way) now ends where it
    stands.
  - `47f122e`.

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
- **More, in `speed.md`:** larger global windows, NVLink frame trains, server islands, active-set
  dispatch and lane-0-only hosts.
