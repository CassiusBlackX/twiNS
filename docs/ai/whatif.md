# What-if runs: no SimAI recording

**Result.** `twins-ai` runs an AI workload on a new network with no SimAI run first:
- It runs astra-sim's real system layer on the CPU and the network on the GPU.
- On 34 of the 35 runs of a matrix of 5 workloads and 7 configurations, every message's start and
  completion is identical to SimAI, to the nanosecond. So is the DAG its system layer produces, line by
  line: every flow, timer, cause and ordinal. The other run did not finish.
- On 41 of the 42 runs of the SimAI paper's workloads, up to 1024 GPUs, the same holds. The other run did
  not finish either.
- Replaying a recorded DAG on another configuration is not exact, with makespan errors up to 26%. That
  is why the closed loop is needed.
- On the SimAI paper's workloads, it is 3–14× faster than SimAI on one core for collectives, 6–9× for
  128-GPU training, and 9–18× for 512- and 1024-GPU training.

Not verified (see "Limits"): GPT-3 175B probe at 1024 GPUs; LLaMA 65B probe on DCN+ with 1 MB buffers.

## Why replay is not enough

A step of a ring collective starts when the previous step's data has arrived and the rank's own send
is done. A recording keeps only whichever came last, and which came last depends on the network. So
the same flows hang from different causes on another configuration.

The experiment records on Spectrum-X (DCQCN, 32 MB) and replays on each other configuration, against
SimAI run directly on that configuration. "Same cause" compares the two recordings.

| workload | config | messages identical | makespan error | median error (ns) | same cause |
|---|---|---|---|---|---|
| microAllReduce, 128 GPUs | AlibabaHPN | 2.43% | −7.01% | 1839 | 29.46% |
| | DCN+ | 1.53% | −2.11% | 2451 | 21.84% |
| | HPCC | 6.48% | +1.34% | 1151 | 38.60% |
| | 200G NICs | 2.18% | +1.37% | 2310 | 26.32% |
| MoE, 16 GPUs | AlibabaHPN | 3.51% | −4.11% | 569124 | 65.46% |
| | DCN+ | 5.60% | +0.01% | 61 | 93.17% |
| | HPCC | 3.69% | +11.68% | 380680 | 67.58% |
| | 200G NICs | 12.41% | +0.02% | 63 | 99.15% |
| AllReduce 64 MB, 128 GPUs | AlibabaHPN | 100% | 0 | 0 | 100% |
| | DCN+ | 14.38% | −20.39% | 14661 | 98.85% |
| | HPCC | 100% | 0 | 0 | 100% |
| | 200G NICs | 100% | 0 | 0 | 100% |
| LLaMA 65B probe, 128 GPUs | AlibabaHPN | 99.998% | +0.53% | 0 | 100% |
| | DCN+ | 76.56% | −25.95% | 0 | 68.20% |
| | HPCC | 100% | 0 | 0 | 100% |
| | 200G NICs | 100% | 0 | 0 | 100% |
| All-to-all 64 MB, 128 GPUs | all of the above | 100% | 0 | 0 | 100% |

Replay is a usable approximation in some of these rows and badly wrong in others, and nothing in the
recording tells which. The DCN+ rows also show that an unchanged cause does not make it exact: AllReduce
keeps 98.85% of its causes and still misses the makespan by 20%.

## How the closed loop works

Code: `src/astra/` (`frontend.cc` between astra-sim and a network, `closed.h` the loop), and
`closed_rec`, `closed_apply`, `closed_sort`, `closed_cmp` in `src/rdma/rstep.cuh`, under `TWINS_ASTRA`.
`twins-rdma` is built without them.

**Why it can be exact.** A completion at t only reaches the network at t + send_lat, because SendFlow
delays the flow's start by that much (3 µs, 120 slots). So the GPU may run up to 119 slots past the
last point the CPU has answered, and nothing the CPU then decides can be late.

**One round.**
1. The GPU runs up to the limit and records each QpComplete and SendComplete with its event's chain.
2. The CPU reads the records and runs everything below the slot the GPU reached, in ns-3's order.
3. Each flow and timer a callback creates is noted with its cause and ordinal.
4. A one-lane kernel builds their chains from their causes' chains, on the GPU, and files the flows.

**ns-3's order.**
- Completions of one nanosecond: sorted on the GPU by their chains (`closed_sort`).
- Timers of one nanosecond: in the order they were scheduled, which the CPU knows.
- A timer against a completion: the larger lead was scheduled earlier and runs first. Equal leads
  are rare (0 to 55 per run); those go to the GPU for a chain comparison (`closed_cmp`).

**Ordinals of an ACK's event.** The GPU continues before the callback's action count is known, so
the event's later children take ordinals from the top half of the range. Only their order among
siblings counts.

**Flows.** The GPU's tables need the flow count up front. A forked child runs the system layer on a
stub network with no congestion, which gives the same (source, destination, size) multiset as SimAI on
every run here. `--flows-cache=F` keeps that list for other configurations of the workload.

**What is not moved to the GPU.** astra-sim runs unmodified, except the two lines SimAI's sequential
build also needs (`src/astra/fetch.sh`). SimAI's code stays out of this repository: the script fetches
it at the validated commit.

## Exactness

SimAI here: `simai-trace64` (64-bit event uids), run directly on each configuration.
`validation/simai/check_whatif.sh closed` runs this.

| | Spectrum-X | AlibabaHPN | DCN+ | HPCC | 200G NICs | 1 MB | DCN+, 1 MB |
|---|---|---|---|---|---|---|---|
| microAllReduce, 128 GPUs (520192) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| MoE, 16 GPUs (6496) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| AllReduce 64 MB, 128 GPUs (3840) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| LLaMA 65B probe, 128 GPUs (439168) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | not finished |
| All-to-all 64 MB, 128 GPUs (1920) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

✓: all messages identical, and no line of the DAG differs (flows, timers, causes, ordinals).
HPCC is CC_MODE 3; 1 MB is BUFFER_SIZE 1.

**What buffers test.** Ring collectives never fill a buffer: at 1 MB, on Spectrum-X and on DCN+, SimAI's
own results equal those at 32 MB, so those cells prove little. All-to-all on DCN+ does respond: at 32 MB
it has 2899 PFC pauses and 34 drops, and at 1 MB every message's time changes (makespan 7.36 → 7.07 ms).
Both are exact.

**On the paper's workloads** (`validation/simai/check_closed.sh validation/simai/jobs_paper.txt`):
41 of 42 runs are identical, messages and DAG lines. The other one, GPT-3 175B probe at 1024 GPUs, did
not finish (see "Limits").

GPT-3 175B full at 128 GPUs first showed 165 messages and most DAG lines differing. Its reference was
32-bit SimAI, and the run passes 2^32 events (see `paper.md`): compared by (source, destination, size,
place), every message was already identical, and only the order of simultaneous completions on different
ranks differed, which renumbers flows and timers. Against SimAI rebuilt with 64-bit uids, all 5972864
messages and every DAG line are identical. Replaying the recording could not show this, since a
recording carries its own numbering.

## Speed

RX 9060 XT at `784c6cf`, SimAI on one core of a87 (untraced where measured, as in `paper.md`). twins-ai's
time is one pass: GPU, system layer, sync and stub. Reruns for more room are not counted; 8 happened.
With `--flows-cache` the stub column drops out.

| run | messages | simulated (ms) | SimAI (s) | replay (s) | twins-ai (s) | GPU | system layer | sync | stub | speedup |
|---|---|---|---|---|---|---|---|---|---|---|
| c_allgather_512m_hpn128 | 1920 | 23.37 | 54.4 | 11.6 | 15.4 | 14.9 | 0.0 | 0.4 | 0.0 | 3.5× |
| c_allgather_64m_hpn128 | 1920 | 2.63 | 10.2 | 1.3 | 1.9 | 1.7 | 0.0 | 0.1 | 0.0 | 5.4× |
| c_allgather_8m_hpn128 | 1920 | 0.40 | 4.8 | 0.2 | 0.3 | 0.2 | 0.0 | 0.0 | 0.0 | 14.5× |
| c_allreduce_512m_hpn128 | 3840 | 55.66 | 102.7 | 25.7 | 34.9 | 33.9 | 0.1 | 0.9 | 0.1 | 2.9× |
| c_allreduce_64m_hpn128 | 3840 | 5.25 | 16.6 | 2.7 | 3.7 | 3.4 | 0.0 | 0.2 | 0.1 | 4.5× |
| c_allreduce_8m_hpn128 | 3840 | 0.80 | 6.2 | 0.4 | 0.6 | 0.5 | 0.0 | 0.1 | 0.1 | 9.7× |
| c_reducescatter_512m_hpn128 | 1920 | 23.37 | 55.2 | 11.4 | 15.5 | 15.0 | 0.0 | 0.4 | 0.0 | 3.6× |
| c_reducescatter_64m_hpn128 | 1920 | 2.63 | 10.2 | 1.3 | 1.9 | 1.7 | 0.0 | 0.1 | 0.0 | 5.4× |
| c_reducescatter_8m_hpn128 | 1920 | 0.40 | 4.8 | 0.2 | 0.3 | 0.2 | 0.0 | 0.0 | 0.0 | 14.3× |
| gpt3_13b_probe_hpn128 | 42240 | 56.37 | 517.8 | 86.1 | 91.8 | 87.8 | 0.9 | 2.4 | 0.7 | 5.6× |
| gpt3_175b_probe_hpn128 | 1385344 | 189.25 | 2125.2 | 166.8 | 226.9 | 190.2 | 15.6 | 7.5 | 13.7 | 9.4× |
| llama_65b_probe_hpn128 | 439168 | 81.35 | 620.4 | 60.4 | 82.5 | 70.8 | 4.7 | 2.7 | 4.2 | 7.5× |
| c_allgather_512m_hpn512 | 32256 | 27.80 | 258.1 | 23.6 | 30.5 | 28.2 | 0.6 | 1.0 | 0.7 | 8.5× |
| c_allgather_64m_hpn512 | 32256 | 3.01 | 39.4 | 2.7 | 4.8 | 3.2 | 0.6 | 0.4 | 0.7 | 8.1× |
| c_allgather_8m_hpn512 | 32256 | 0.67 | 13.0 | 0.4 | 1.9 | 0.5 | 0.6 | 0.2 | 0.7 | 6.7× |
| c_allreduce_512m_hpn512 | 64512 | 108.05 | 490.0 | 75.8 | 99.1 | 93.5 | 1.3 | 3.0 | 1.2 | 4.9× |
| c_allreduce_64m_hpn512 | 64512 | 6.02 | 74.1 | 5.5 | 9.6 | 6.5 | 1.2 | 0.6 | 1.2 | 7.8× |
| c_allreduce_8m_hpn512 | 64512 | 1.35 | 19.0 | 0.8 | 3.8 | 1.0 | 1.2 | 0.4 | 1.2 | 5.0× |
| c_reducescatter_512m_hpn512 | 32256 | 27.75 | 250.8 | 23.7 | 30.6 | 28.3 | 0.6 | 1.1 | 0.7 | 8.2× |
| c_reducescatter_64m_hpn512 | 32256 | 3.01 | 38.7 | 2.8 | 4.9 | 3.2 | 0.6 | 0.4 | 0.7 | 7.9× |
| c_reducescatter_8m_hpn512 | 32256 | 0.67 | 12.9 | 0.4 | 2.0 | 0.5 | 0.6 | 0.2 | 0.7 | 6.5× |
| c_allgather_512m_hpn1024 | 130048 | 29.95 | 542.8 | 30.7 | 44.8 | 35.6 | 3.4 | 2.0 | 3.7 | 12.1× |
| c_allgather_64m_hpn1024 | 130048 | 3.37 | 92.4 | 3.6 | 12.6 | 4.6 | 3.4 | 0.8 | 3.8 | 7.3× |
| c_allgather_8m_hpn1024 | 130048 | 1.02 | 34.2 | 0.6 | 8.9 | 0.9 | 3.4 | 0.9 | 3.7 | 3.8× |
| c_allreduce_512m_hpn1024 | 260096 | 172.60 | 1079.0 | 129.2 | 180.8 | 160.0 | 7.3 | 6.2 | 7.3 | 6.0× |
| c_allreduce_64m_hpn1024 | 260096 | 6.75 | 171.6 | 7.3 | 25.2 | 9.4 | 6.9 | 1.6 | 7.2 | 6.8× |
| c_allreduce_8m_hpn1024 | 260096 | 2.04 | 53.2 | 1.2 | 18.1 | 1.9 | 7.2 | 1.6 | 7.4 | 2.9× |
| c_reducescatter_512m_hpn1024 | 130048 | 43.90 | 530.5 | 37.7 | 55.0 | 45.3 | 3.5 | 2.3 | 3.8 | 9.7× |
| c_reducescatter_64m_hpn1024 | 130048 | 3.38 | 86.4 | 3.6 | 12.8 | 4.7 | 3.4 | 0.9 | 3.8 | 6.8× |
| c_reducescatter_8m_hpn1024 | 130048 | 1.02 | 32.1 | 0.6 | 9.2 | 0.9 | 3.5 | 1.0 | 3.8 | 3.5× |
| gpt3_13b_full_hpn128 | 120064 | 557.97 | 5220.5 | 824.3 | 860.4 | 844.5 | 1.9 | 12.4 | 1.5 | 6.1× |
| llama_65b_full_hpn128 | 4797312 | 904.12 | 6673.1 | 639.0 | 892.7 | 763.4 | 54.5 | 26.7 | 48.1 | 7.5× |
| gpt3_175b_full_hpn128 | 5972864 | 734.78 | 6384.6 | 566.2 | 826.8 | 669.4 | 68.0 | 29.5 | 60.0 | 7.7× |
| gpt3_13b_probe_hpn512 | 562176 | 73.74 | 2986.5 | 189.6 | 247.8 | 193.3 | 23.9 | 6.9 | 23.7 | 12.1× |
| llama_65b_probe_hpn512 | 1781248 | 153.75 | 3526.2 | 170.1 | 250.7 | 198.0 | 23.2 | 9.0 | 20.5 | 14.1× |
| llama_65b_mid_hpn512 | 2698752 | 244.28 | 5380.8 | 260.4 | 385.1 | 304.9 | 35.8 | 13.1 | 31.3 | 14.0× |
| gpt3_13b_probe_hpn1024 | 2172928 | 80.32 | 6139.4 | 334.1 | 659.3 | 332.8 | 156.8 | 14.5 | 155.2 | 9.3× |
| gpt3_13b_mid_hpn1024 | 2271232 | 184.44 | 16032.2 | 736.6 | 1075.5 | 735.0 | 162.4 | 21.5 | 156.6 | 14.9× |
| llama_65b_mid_hpn1024 | 5463040 | 338.37 | 12517.3 | 446.0 | 688.5 | 514.4 | 81.6 | 25.3 | 67.3 | 18.2× |
| gpt3_13b_mid_hpn512 | 611328 | 175.93 | 7156.8 | 474.7 | 537.6 | 476.2 | 25.2 | 12.1 | 24.1 | 13.3× |
| llama_65b_probe_hpn1024 | 3628032 | 183.12 | 7026.8 | 273.1 | 425.6 | 309.7 | 54.0 | 16.8 | 45.2 | 16.5× |

**What sets the speedup.**
- GPU time is close to replay's: the rounds add little, except `rdma_rank`, which the loop leaves out.
- The system layer costs 3–13 µs per callback on one CPU core, and the stub about the same again. Small
  messages at scale spend most of their time there: 8 MB AllReduce at 1024 GPUs is 2.9× instead of
  replay's 43×.

## Limits

- **GPT-3 175B probe, 1024 GPUs (11.2 M messages): not verified.** It needs a larger DAG message buffer
  and a larger tail ring than the default. `--tail=26` did not fit the GPU's 16 GB next to 11 M flows.
  A run at `--dcap=16384 --tail=25` was stopped because the machine ran low on memory: the system layer
  alone takes about 13 GB of host memory here, on a 31 GB desktop. Its replay is identical (`paper.md`).
- **LLaMA 65B probe, DCN+, 1 MB: not finished.** It reached about 20% of its slots in the one hour it
  was given, and replay is slow there too, so this is TwiNS's network path, not the loop. The same point
  takes 126 s at 8 MB and 2518 s at 1 MB; 2 and 4 MB are slow as well. The likely reason is that wide
  switches lose their fast path once the buffer cannot cover the ports' reserves, which is not
  confirmed. SimAI's result at 1 MB equals its result at 32 MB. Exactness at small buffers is shown by
  the all-to-all, and speed there is an open problem.
- Only what these workloads use has been compared: ring collectives, and all-to-all. Tree, NVLS and PAT
  are not covered.
- A uniform send_lat is assumed (`SEND_LAT_NS`, SimAI's `AS_SEND_LAT`). Windows rely on it, and so does
  the flow filing. SimAI's per-algorithm table is not modelled.
- CC is what twins-rdma models: DCQCN and HPCC.
- Each callback runs on one CPU thread, so microAllReduce-like loads stay near SimAI's own speed.

## Fixes the matrix found in TwiNS

Replaying a run's own DAG on these configurations found three bugs that the paper's runs had not hit.
All three affected exactness:
- **Global windows.** After an idle jump, the next launch used horizons read before a flow fired, and
  could contain that flow's start. DCQCN's timers had hidden this; HPCC jumps. A launch after a jump is
  now a single slot.
- **Ordinals.** They saturated at 7, tying the siblings of a tick that starts more than 8 flows. They
  have 5 bits now.
- **Lost tails.** A tie on an overwritten chain tail is not exact. The run now starts again with more
  room, as `--dcap` does.
