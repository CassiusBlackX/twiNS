# What-if runs: no SimAI recording

**Result.** `twins-ai` runs an AI workload on a new network with no SimAI run first:
- It runs astra-sim's real system layer on the CPU and the network on the GPU.
- On all 47 runs of a matrix of 5 workloads and 10 configurations, every message's start and completion
  is identical to SimAI, to the nanosecond. So is the DAG its system layer produces, line by line: every
  flow, timer, cause and ordinal.
- On all 42 runs of the SimAI paper's workloads, up to 1024 GPUs, the same holds.
- Replaying a recorded DAG on another configuration is not exact, with makespan errors up to 26%. That
  is why the closed loop is needed.
- On the SimAI paper's workloads, it is 3–12× faster than SimAI on one core for collectives, 5–8× for
  128-GPU training, and 9–18× for 512- and 1024-GPU training.

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

| | Spectrum-X | AlibabaHPN | DCN+ | HPCC | 200G NICs | 1 MB | 2 MB | DCN+, 1 MB | DCN+, 2 MB | DCN+, 4 MB |
|---|---|---|---|---|---|---|---|---|---|---|
| microAllReduce, 128 GPUs (520192) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | no reference | ✓ |
| MoE, 16 GPUs (6496) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | no reference | ✓ |
| AllReduce 64 MB, 128 GPUs (3840) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | no reference | ✓ |
| LLaMA 65B probe, 128 GPUs (439168) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| All-to-all 64 MB, 128 GPUs (1920) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

✓: all messages identical, and no line of the DAG differs (flows, timers, causes, ordinals).
HPCC is CC_MODE 3; 1, 2 and 4 MB are BUFFER_SIZE on Spectrum-X or DCN+. "No reference": SimAI was not run
there.

**What buffers test.** Few of the small-buffer cells test PFC, because of a quirk in SimAI's ns-3. The
threshold is 32 bits unsigned:

```
uint32_t SwitchMmu::GetPfcThreshold(uint32_t port){
    return (buffer_size - total_hdrm - total_rsrv - shared_used_bytes) >> pfc_a_shift[port];
}
```

When the buffer is smaller than the switch's headroom and reserves, the sum wraps to about 2^32. That
switch then never pauses and never drops, so a "1 MB" buffer on a wide switch behaves as an infinite one.
TwiNS reproduces this, as exactness requires.

| switch | headroom + reserves (MB) |
|---|---|
| DCN+, 128 GPUs, 72 ports | 3.14 |
| Spectrum-X, 128 GPUs, 80 ports | 6.03 |
| 200G NICs, 128 GPUs | 5.46 |
| AlibabaHPN, 128 GPUs, 136 ports | 9.69 |
| AlibabaHPN, 512 GPUs | 11.59 |
| AlibabaHPN, 1024 GPUs | 14.13 |

What the cells therefore show:
- **1 and 2 MB, on Spectrum-X and on DCN+:** the wide switches wrap, so these cells prove little about
  PFC. All-to-all on DCN+ finishes earlier at 1 MB than at 32 MB (7.07 ms against 7.36 ms, with 2899
  pauses and 34 drops at 32 MB), and 1 and 2 MB give the same 7.07 ms.
- **32 MB:** no configuration wraps.
- **DCN+, 4 MB:** this is the cell where the buffer really is small, and it is exact, including the flows
  that never finish. SimAI strands the same flows after the drops. Why they strand was not investigated.

| DCN+, 4 MB | drops | pauses | NACKs | flows finished |
|---|---|---|---|---|
| LLaMA 65B probe | 40 | 982827 | 34 | 439089 of 439168 |
| All-to-all 64 MB | 1178 | 284617 | 933 | 1879 of 1920 |
| AllReduce 64 MB | 21 | 5072 | 14 | 3575 of 3840 |
| microAllReduce, MoE | 0 | 0 | 0 | all |

**On the paper's workloads** (`validation/simai/check_closed.sh validation/simai/jobs_paper.txt`):
all 42 runs are identical, messages and DAG lines. GPT-3 175B probe at 1024 GPUs needs options of its
own (see "Limits"): all 11197440 messages are identical, with no line of the DAG differing, and no
undecided ties or lost tails.

GPT-3 175B full at 128 GPUs first showed 165 messages and most DAG lines differing. Its reference was
32-bit SimAI, and the run passes 2^32 events (see `paper.md`): compared by (source, destination, size,
place), every message was already identical, and only the order of simultaneous completions on different
ranks differed, which renumbers flows and timers. Against SimAI rebuilt with 64-bit uids, all 5972864
messages and every DAG line are identical. Replaying the recording could not show this, since a
recording carries its own numbering.

## Speed

RX 9060 XT at `2e156c4`, SimAI on one core of a87 (untraced where measured, as in `paper.md`). twins-ai's
time is one pass: GPU, system layer, sync and stub. Reruns for more room are not counted; 9 happened.
With `--flows-cache` the stub column drops out. The machine drifts by 10–15% between runs, so small
differences between rows or from earlier builds mean little.

| run | messages | simulated (ms) | SimAI (s) | replay (s) | twins-ai (s) | GPU | system layer | sync | stub | speedup |
|---|---|---|---|---|---|---|---|---|---|---|
| c_allgather_512m_hpn128 | 1920 | 23.37 | 54.4 | 11.6 | 15.7 | 15.2 | 0.0 | 0.5 | 0.0 | 3.5× |
| c_allgather_64m_hpn128 | 1920 | 2.63 | 10.2 | 1.3 | 2.0 | 1.8 | 0.0 | 0.1 | 0.0 | 5.0× |
| c_allgather_8m_hpn128 | 1920 | 0.40 | 4.8 | 0.2 | 0.5 | 0.4 | 0.0 | 0.1 | 0.0 | 9.4× |
| c_allreduce_512m_hpn128 | 3840 | 55.66 | 102.7 | 25.7 | 36.5 | 35.4 | 0.1 | 1.0 | 0.1 | 2.8× |
| c_allreduce_64m_hpn128 | 3840 | 5.25 | 16.6 | 2.7 | 4.0 | 3.7 | 0.0 | 0.2 | 0.1 | 4.1× |
| c_allreduce_8m_hpn128 | 3840 | 0.80 | 6.2 | 0.4 | 0.9 | 0.7 | 0.0 | 0.1 | 0.1 | 7.1× |
| c_reducescatter_512m_hpn128 | 1920 | 23.37 | 55.2 | 11.4 | 15.9 | 15.3 | 0.0 | 0.5 | 0.0 | 3.5× |
| c_reducescatter_64m_hpn128 | 1920 | 2.63 | 10.2 | 1.3 | 2.1 | 1.9 | 0.0 | 0.1 | 0.0 | 4.8× |
| c_reducescatter_8m_hpn128 | 1920 | 0.40 | 4.8 | 0.2 | 0.5 | 0.3 | 0.0 | 0.1 | 0.0 | 10.6× |
| gpt3_13b_probe_hpn128 | 42240 | 56.37 | 517.8 | 86.1 | 94.5 | 90.3 | 0.9 | 2.5 | 0.7 | 5.5× |
| gpt3_175b_probe_hpn128 | 1385344 | 189.25 | 2125.2 | 166.8 | 259.5 | 217.7 | 17.5 | 9.9 | 14.5 | 8.2× |
| llama_65b_probe_hpn128 | 439168 | 81.35 | 620.4 | 60.4 | 85.5 | 73.3 | 4.8 | 3.0 | 4.3 | 7.3× |
| c_allgather_512m_hpn512 | 32256 | 27.80 | 258.1 | 23.6 | 31.1 | 28.7 | 0.6 | 1.1 | 0.7 | 8.3× |
| c_allgather_64m_hpn512 | 32256 | 3.01 | 39.4 | 2.7 | 5.3 | 3.6 | 0.6 | 0.4 | 0.7 | 7.5× |
| c_allgather_8m_hpn512 | 32256 | 0.67 | 13.0 | 0.4 | 2.5 | 0.9 | 0.6 | 0.4 | 0.7 | 5.2× |
| c_allreduce_512m_hpn512 | 64512 | 108.05 | 490.0 | 75.8 | 99.8 | 94.0 | 1.4 | 3.2 | 1.2 | 4.9× |
| c_allreduce_64m_hpn512 | 64512 | 6.02 | 74.1 | 5.5 | 10.0 | 6.9 | 1.2 | 0.7 | 1.2 | 7.4× |
| c_allreduce_8m_hpn512 | 64512 | 1.35 | 19.0 | 0.8 | 4.9 | 1.9 | 1.2 | 0.6 | 1.2 | 3.8× |
| c_reducescatter_512m_hpn512 | 32256 | 27.75 | 250.8 | 23.7 | 31.3 | 28.8 | 0.6 | 1.1 | 0.7 | 8.0× |
| c_reducescatter_64m_hpn512 | 32256 | 3.01 | 38.7 | 2.8 | 5.4 | 3.6 | 0.6 | 0.5 | 0.7 | 7.2× |
| c_reducescatter_8m_hpn512 | 32256 | 0.67 | 12.9 | 0.4 | 2.6 | 0.9 | 0.6 | 0.4 | 0.7 | 5.0× |
| c_allgather_512m_hpn1024 | 130048 | 29.95 | 542.8 | 30.7 | 45.7 | 36.4 | 3.4 | 2.1 | 3.8 | 11.9× |
| c_allgather_64m_hpn1024 | 130048 | 3.37 | 92.4 | 3.6 | 13.6 | 5.5 | 3.4 | 1.0 | 3.8 | 6.8× |
| c_allgather_8m_hpn1024 | 130048 | 1.02 | 34.2 | 0.6 | 9.8 | 1.4 | 3.5 | 1.2 | 3.8 | 3.5× |
| c_allreduce_512m_hpn1024 | 260096 | 172.60 | 1079.0 | 129.2 | 183.6 | 162.4 | 7.3 | 6.5 | 7.4 | 5.9× |
| c_allreduce_64m_hpn1024 | 260096 | 6.75 | 171.6 | 7.3 | 27.9 | 11.8 | 7.0 | 1.8 | 7.2 | 6.2× |
| c_allreduce_8m_hpn1024 | 260096 | 2.04 | 53.2 | 1.2 | 20.4 | 2.6 | 7.7 | 2.2 | 7.9 | 2.6× |
| c_reducescatter_512m_hpn1024 | 130048 | 43.90 | 530.5 | 37.7 | 57.1 | 46.7 | 3.8 | 2.5 | 4.1 | 9.3× |
| c_reducescatter_64m_hpn1024 | 130048 | 3.38 | 86.4 | 3.6 | 16.9 | 7.6 | 3.8 | 1.3 | 4.2 | 5.1× |
| c_reducescatter_8m_hpn1024 | 130048 | 1.02 | 32.1 | 0.6 | 10.8 | 1.4 | 3.9 | 1.4 | 4.1 | 3.0× |
| gpt3_13b_full_hpn128 | 120064 | 557.97 | 5220.5 | 824.3 | 884.2 | 866.7 | 2.3 | 13.6 | 1.7 | 5.9× |
| llama_65b_full_hpn128 | 4797312 | 904.12 | 6673.1 | 639.0 | 971.1 | 824.0 | 61.2 | 31.5 | 54.4 | 6.9× |
| gpt3_175b_full_hpn128 | 5972864 | 734.78 | 6384.6 | 566.2 | 832.2 | 677.5 | 66.7 | 29.8 | 58.2 | 7.7× |
| gpt3_13b_probe_hpn512 | 562176 | 73.74 | 2986.5 | 189.6 | 248.1 | 194.5 | 23.3 | 6.9 | 23.5 | 12.0× |
| llama_65b_probe_hpn512 | 1781248 | 153.75 | 3526.2 | 170.1 | 252.6 | 200.1 | 23.1 | 9.1 | 20.3 | 14.0× |
| llama_65b_mid_hpn512 | 2698752 | 244.28 | 5380.8 | 260.4 | 388.1 | 308.3 | 35.5 | 13.2 | 31.2 | 13.9× |
| gpt3_13b_probe_hpn1024 | 2172928 | 80.32 | 6139.4 | 334.1 | 661.5 | 335.1 | 156.3 | 14.5 | 155.7 | 9.3× |
| gpt3_13b_mid_hpn1024 | 2271232 | 184.44 | 16032.2 | 736.6 | 1077.5 | 740.6 | 159.1 | 21.6 | 156.2 | 14.9× |
| llama_65b_mid_hpn1024 | 5463040 | 338.37 | 12517.3 | 446.0 | 694.3 | 522.5 | 79.6 | 25.3 | 66.9 | 18.0× |
| gpt3_13b_mid_hpn512 | 611328 | 175.93 | 7156.8 | 474.7 | 540.5 | 479.7 | 24.6 | 12.2 | 24.0 | 13.2× |
| llama_65b_probe_hpn1024 | 3628032 | 183.12 | 7026.8 | 273.1 | 428.8 | 314.6 | 52.6 | 16.9 | 44.7 | 16.4× |
| gpt3_175b_probe_hpn1024 | 11197440 | 527.91 | 23325.9 | 844.8 | 1575.5 | 1164.4 | 187.2 | 73.7 | 150.2 | 14.8× |

**What sets the speedup.**
- GPU time is close to replay's: the rounds add little, except `rdma_rank`, which the loop leaves out.
- The system layer costs 3–13 µs per callback on one CPU core, and the stub about the same again. Small
  messages at scale spend most of their time there: 8 MB AllReduce at 1024 GPUs is 2.6× instead of
  replay's 43×.

Replay still works after these changes: `twins-rdma`, built from the same sources, reproduces 41 of the
42 paper runs message for message (`gpt3_175b_full_hpn128` on its 64-bit DAG). The other,
`gpt3_175b_probe_hpn1024`, was left out for its memory use; its identical result in `paper.md` comes
from an earlier build. Those runs were on `bb6c863`. On `2e156c4`, four replays were rerun, with the same
output as before.

## Limits

- **GPT-3 175B probe, 1024 GPUs (11.2 M messages) needs `--dcap=16384 --tail=25`.** `--tail=26` does not
  fit the GPU's 16 GB next to 11 M flows, and the automatic reruns would reach it. The run peaks at 18.3 GB
  of host memory and 3.1 GB of swap, with about 11 GB of GPU driver and kernel memory on top, outside the
  process's cgroup. Forbidding swap on a 31 GB machine ends in an out-of-memory kill.
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

## Two fixes for speed at small buffers

LLaMA 65B probe on DCN+ at 1 MB did not finish in an hour, and was much slower at 2 and 4 MB too. Both
causes were in TwiNS, and neither changed a result: the matrix and the paper's runs above are on the fixed
build, and the 229 runs and worlds of the gate pass.

4 MB does not wrap on DCN+, so only the second fix applies there.

| LLaMA 65B probe, DCN+ | before (s) | wrap fixed (s) | both fixed (s) | SimAI (s) |
|---|---|---|---|---|
| 1 MB | over 3600, at about 20% of its slots | 393 | 185 | 618 |
| 2 MB | not measured | not measured | 184 | 637 |
| 4 MB | not measured | 3775 | 268 | 665 |

- **Safe slots ignored the wrap.** A wide switch's slot runs in parallel only if no port can cross the
  PFC threshold. The check computed the threshold's room as a signed number, so a wrapped threshold looked
  negative and every slot of that switch looked unsafe. It now wraps as SimAI does
  (`switch_step` in `src/rdma/rstep.cuh`).
- **Long chain walks repeated themselves.** Under heavy PFC at 4 MB, comparing two events' chains took
  deep walks that decide by lead, about 5000 runs each, and the memo saw only the chains' top levels. In a
  30 ms profile of 4 MB, 794 thousand walks took 1.4 G steps. `ccmp_walk` now remembers sampled states from
  the walk's tail (a direct-mapped table of 2^22 entries, 32 MB). The same profile went from 873 s to 53 s
  and from 1.4 G steps to 6 M, with the same pauses and events.
- **The cost.** Runs that do not need this are at most about 1.2% slower: in interleaved replays,
  `gpt3_13b_mid_hpn512` took 484.1 s against 478.6 s, and three others differed by less. Their output was
  the same.
