# Why twins-ai was not faster, and what changed

**Result.** On the 42 paper runs, one RX 9060 XT against SimAI on one core, wall clock to wall clock:

| runs | before | now | target set beforehand |
|---|---|---|---|
| collectives, 128 GPUs | 2.8–4.8× | 6.9–10.0× | 10–30× |
| collectives, 512 GPUs | 3.2–8.1× | 7.6–24.8× | 10–30× |
| collectives, 1024 GPUs | 2.4–11.5× | 6.1–27.7× | 10–30× |
| training, 128 GPUs | 3.2–8.1× | 5.8–13.8× | 15–25× |
| training, 512 GPUs | 9.0–13.8× | 13.9–23.7× | — |
| training, 1024 GPUs | 9.2–14.8× | 15.0–28.8× | 40–60× |

The whole set takes 6323 s instead of 12861 s. All 42 runs and all 47 runs of the what-if matrix stay
identical to SimAI, per message and per DAG line.

**The targets were estimates, and most were missed.** They assumed about 0.5 µs per frame everywhere.
GPT-3 13B at 128 GPUs stays at 6×, because almost all of its time is single-slot launches, each bound by
one slow wave. Nothing tried here changes that (see "What still limits it").

**Replay, on the same runs, shows the GPU's share alone** (simulation loop, without start-up): collectives
27–122× at 1024 GPUs, from 8–57×; LLaMA 65B and GPT-3 175B 15–44×, from 10–28×; **GPT-3 13B unchanged**, at
6–22×. So 13B's closed-loop gains are CPU time and reruns, none of it from the GPU.

## Why it was slow

Measured before the changes: frames per slot, the share of time with an NVLink busy, and the GPU time per
frame against SimAI's total per frame.

| run | frames/slot | NVLink busy | µs/slot | TwiNS µs/frame | SimAI µs/frame |
|---|---|---|---|---|---|
| `c_allreduce_512m_hpn128` | 6.4 | 0% | 15.9 | 2.47 | 7.17 |
| `llama_65b_probe_hpn128` | 32 | 20.7% | 22.5 | 0.70 | 5.92 |
| `gpt3_13b_probe_hpn128` | 32 | 89.8% | 40.0 | 1.25 | 7.18 |
| `gpt3_13b_mid_hpn1024` | 188 | 98.6% | 100.4 | 0.53 | 11.54 |

Four causes:

1. **Runs started over.** The old speed table counted one pass. Runs that ran out of DAG room or tails
   restarted from nothing, 9 times over the set, for about 3100 of the 12861 s. LLaMA 65B full at 128 GPUs
   took 2055 s for a 971 s pass. Only the logs' wall time showed this.
2. **A global window was still walked slot by slot.** A 20-slot window ran 20 steps per entity, with a
   `__syncthreads` and global reads in each. Most were empty: at 128 GPUs, 88% of host steps had no event,
   at 4864 cycles each. A launch's time is set by its slowest wave, which at 512 MB AllReduce was 67 G
   cycles over the run.
3. **The CPU.** The system layer copied structures it only read, and scanned every stream for each
   channel. The stub repeated all that. GPT-3 13B probe at 1024 GPUs spent 312 of its 661 s there.
4. **A slow event, in single-slot launches.** See "What still limits it".

## What changed

| commit | change | before → final code |
|---|---|---|
| `6c2763b` | A global window runs as one step: a slot only for an entity with something in it | 512 MB AllReduce at 128 GPUs, GPU time 35.4 → 11.1 s |
| `2b362e1` | Closed loop: no launch that cannot run anything, fewer syncs. System layer: no copies of what it only reads, `-O3`, tcmalloc | LLaMA 65B probe at 128 GPUs, 86 → 50 s, with `6c2763b`; GPT-3 13B probe at 1024 GPUs, stub 156 → 55 s |
| `056db5b` | A switch marked busy with nothing to come takes a step in its window | fixes a hang from the changes above (see below) |
| `75d050b` | Large runs start with the room they need: DAG room 16384, tails 2^27 from 2 M flows, as VRAM allows | no reruns on any of the 42 runs; GPT-3 175B probe at 1024 GPUs needs no options |
| `9829a00` | System layer: per-channel lists in `StreamInit`, counters instead of `free_packets` scans, no detailed-flow dump | the same stub, 55 → 23 s alone, 27 s in the final run |

The system layer's behaviour is unchanged: per-DAG-line comparison covers this. `src/astra/fetch.sh`
applies these edits from pristine sources.

## Run by run

"Before" is `2e156c4`, wall clock with its reruns (GPT-3 175B probe with `--dcap=16384 --tail=25`). "Now"
is wall clock with no reruns. Machine drift is 10–15%. Runs of about 1 s are mostly start-up: about 0.5 s
at 128 GPUs and 1.4 s at 1024.

| run | messages | simulated (ms) | SimAI (s) | before (s) | now (s) | GPU | system layer | sync | stub | before × | now × |
|---|---|---|---|---|---|---|---|---|---|---|---|
| c_allgather_512m_hpn128 | 1920 | 23.37 | 54.4 | 16.5 | 5.5 | 4.7 | 0.0 | 0.3 | 0.0 | 3.3× | 9.9× |
| c_allgather_64m_hpn128 | 1920 | 2.63 | 10.2 | 2.6 | 1.1 | 0.5 | 0.0 | 0.1 | 0.0 | 3.9× | 9.3× |
| c_allgather_8m_hpn128 | 1920 | 0.40 | 4.8 | 1.1 | 0.7 | 0.1 | 0.0 | 0.0 | 0.0 | 4.4× | 6.9× |
| c_allreduce_512m_hpn128 | 3840 | 55.66 | 102.7 | 37.1 | 12.2 | 11.1 | 0.0 | 0.5 | 0.0 | 2.8× | 8.4× |
| c_allreduce_64m_hpn128 | 3840 | 5.25 | 16.6 | 4.6 | 1.7 | 1.0 | 0.0 | 0.1 | 0.0 | 3.6× | 9.8× |
| c_allreduce_8m_hpn128 | 3840 | 0.80 | 6.2 | 1.4 | 0.8 | 0.1 | 0.0 | 0.1 | 0.0 | 4.4× | 7.8× |
| c_reducescatter_512m_hpn128 | 1920 | 23.37 | 55.2 | 16.5 | 5.5 | 4.7 | 0.0 | 0.2 | 0.0 | 3.3× | 10.0× |
| c_reducescatter_64m_hpn128 | 1920 | 2.63 | 10.2 | 2.7 | 1.2 | 0.5 | 0.0 | 0.1 | 0.0 | 3.8× | 8.5× |
| c_reducescatter_8m_hpn128 | 1920 | 0.40 | 4.8 | 1.0 | 0.7 | 0.1 | 0.0 | 0.0 | 0.0 | 4.8× | 6.9× |
| gpt3_13b_probe_hpn128 | 42240 | 56.37 | 517.8 | 95.1 | 89.9 | 86.6 | 0.4 | 2.0 | 0.3 | 5.4× | 5.8× |
| gpt3_175b_probe_hpn128 | 1385344 | 189.25 | 2125.2 | 261.9 | 154.2 | 132.3 | 7.3 | 5.5 | 6.8 | 8.1× | 13.8× |
| llama_65b_probe_hpn128 | 439168 | 81.35 | 620.4 | 86.5 | 49.9 | 42.4 | 2.3 | 2.0 | 2.2 | 7.2× | 12.4× |
| c_allgather_512m_hpn512 | 32256 | 27.80 | 258.1 | 32.0 | 10.4 | 8.2 | 0.2 | 0.8 | 0.3 | 8.1× | 24.8× |
| c_allgather_64m_hpn512 | 32256 | 3.01 | 39.4 | 6.1 | 2.6 | 0.9 | 0.2 | 0.3 | 0.3 | 6.5× | 15.2× |
| c_allgather_8m_hpn512 | 32256 | 0.67 | 13.0 | 3.4 | 1.7 | 0.2 | 0.2 | 0.1 | 0.3 | 3.8× | 7.6× |
| c_allreduce_512m_hpn512 | 64512 | 108.05 | 490.0 | 100.7 | 35.2 | 31.0 | 0.5 | 2.2 | 0.5 | 4.9× | 13.9× |
| c_allreduce_64m_hpn512 | 64512 | 6.02 | 74.1 | 10.9 | 4.2 | 1.8 | 0.4 | 0.5 | 0.5 | 6.8× | 17.6× |
| c_allreduce_8m_hpn512 | 64512 | 1.35 | 19.0 | 5.9 | 2.5 | 0.4 | 0.4 | 0.3 | 0.5 | 3.2× | 7.6× |
| c_reducescatter_512m_hpn512 | 32256 | 27.75 | 250.8 | 32.1 | 10.6 | 8.4 | 0.2 | 0.8 | 0.3 | 7.8× | 23.7× |
| c_reducescatter_64m_hpn512 | 32256 | 3.01 | 38.7 | 6.2 | 2.6 | 0.9 | 0.2 | 0.3 | 0.3 | 6.2× | 14.9× |
| c_reducescatter_8m_hpn512 | 32256 | 0.67 | 12.9 | 3.4 | 1.7 | 0.2 | 0.2 | 0.2 | 0.3 | 3.8× | 7.6× |
| c_allgather_512m_hpn1024 | 130048 | 29.95 | 542.8 | 47.2 | 19.6 | 13.9 | 1.0 | 1.9 | 1.3 | 11.5× | 27.7× |
| c_allgather_64m_hpn1024 | 130048 | 3.37 | 92.4 | 15.1 | 6.2 | 1.7 | 1.0 | 0.7 | 1.3 | 6.1× | 14.9× |
| c_allgather_8m_hpn1024 | 130048 | 1.02 | 34.2 | 11.3 | 4.8 | 0.4 | 1.0 | 0.6 | 1.3 | 3.0× | 7.1× |
| c_allreduce_512m_hpn1024 | 260096 | 172.60 | 1079.0 | 185.3 | 72.1 | 60.5 | 2.4 | 5.1 | 2.4 | 5.8× | 15.0× |
| c_allreduce_64m_hpn1024 | 260096 | 6.75 | 171.6 | 29.6 | 11.3 | 3.6 | 2.3 | 1.4 | 2.4 | 5.8× | 15.2× |
| c_allreduce_8m_hpn1024 | 260096 | 2.04 | 53.2 | 22.3 | 8.7 | 0.9 | 2.4 | 1.3 | 2.4 | 2.4× | 6.1× |
| c_reducescatter_512m_hpn1024 | 130048 | 43.90 | 530.5 | 58.7 | 22.3 | 16.3 | 1.1 | 2.1 | 1.3 | 9.0× | 23.8× |
| c_reducescatter_64m_hpn1024 | 130048 | 3.38 | 86.4 | 18.5 | 6.4 | 1.7 | 1.0 | 0.8 | 1.4 | 4.7× | 13.5× |
| c_reducescatter_8m_hpn1024 | 130048 | 1.02 | 32.1 | 12.4 | 5.2 | 0.4 | 1.2 | 0.7 | 1.3 | 2.6× | 6.2× |
| gpt3_13b_full_hpn128 | 120064 | 557.97 | 5220.5 | 885.0 | 835.3 | 821.7 | 1.0 | 11.0 | 0.8 | 5.9× | 6.2× |
| llama_65b_full_hpn128 | 4797312 | 904.12 | 6673.1 | 2054.6 | 503.1 | 427.9 | 25.7 | 19.1 | 23.7 | 3.2× | 13.3× |
| gpt3_175b_full_hpn128 | 5972864 | 734.78 | 6384.6 | 1869.2 | 499.9 | 407.8 | 31.9 | 22.2 | 29.6 | 3.4× | 12.8× |
| gpt3_13b_probe_hpn512 | 562176 | 73.74 | 2986.5 | 249.9 | 214.2 | 194.5 | 6.1 | 6.0 | 5.9 | 12.0× | 13.9× |
| llama_65b_probe_hpn512 | 1781248 | 153.75 | 3526.2 | 255.6 | 153.7 | 121.6 | 10.9 | 7.9 | 10.4 | 13.8× | 22.9× |
| llama_65b_mid_hpn512 | 2698752 | 244.28 | 5380.8 | 599.0 | 227.3 | 179.7 | 16.5 | 11.3 | 15.6 | 9.0× | 23.7× |
| gpt3_13b_probe_hpn1024 | 2172928 | 80.32 | 6139.4 | 666.6 | 409.4 | 336.3 | 27.6 | 13.7 | 26.7 | 9.2× | 15.0× |
| gpt3_13b_mid_hpn1024 | 2271232 | 184.44 | 16032.2 | 1082.8 | 823.0 | 741.3 | 29.2 | 19.6 | 27.6 | 14.8× | 19.5× |
| llama_65b_mid_hpn1024 | 5463040 | 338.37 | 12517.3 | 1177.9 | 434.6 | 332.0 | 38.1 | 23.0 | 33.2 | 10.6× | 28.8× |
| gpt3_175b_probe_hpn1024 | 11197440 | 527.91 | 23325.9 | 1593.2 | 887.4 | 677.5 | 83.9 | 41.0 | 68.5 | 14.6× | 26.3× |
| gpt3_13b_mid_hpn512 | 611328 | 175.93 | 7156.8 | 542.4 | 504.0 | 478.8 | 6.9 | 10.1 | 6.2 | 13.2× | 14.2× |
| llama_65b_probe_hpn1024 | 3628032 | 183.12 | 7026.8 | 757.1 | 279.5 | 210.7 | 25.0 | 15.6 | 22.4 | 9.3× | 25.1× |

## What still limits it

- **GPT-3 13B at 128 GPUs (5.8×).** NVLink is busy for 90% of the time, so launches are 1.1 slots. A host
  step with events costs about 75000 cycles for 2.9 events: 26000 per event. The slowest wave of a launch
  is about 110000 cycles, and those waves add up to 81 of the 92 s. It is latency, not throughput: wave
  traces show 40.7% of cycles in `host_step`, flat, and about 36% in memory waits (`s_wait_loadcnt` 16%,
  `loadcnt_dscnt` 9.3%, `kmcnt` 7%, `dscnt` 3.6%). There is no single hot spot left to remove.
- **Small collectives at 1024 GPUs (6.1×).** GPU time is only 0.9 of 8.7 s. The rest is the system layer,
  sync, the stub and start-up, on one i5-7500 core. Replay takes 2.3 s.
- **Training at 1024 GPUs (15–29×).** GPU time is 75–90% of the wall clock. GPT-3 13B again costs the most:
  frames are few and spread out, and NVLink stays busy.

## The bound: the critical path

`-DRDMA_CRIT` (a counting build; results unchanged) gives each event a depth: one more than the
larger of its scheduler's depth (carried in its chain) and the previous event's at its node. The
deepest event is the run's critical path, the most any exact execution must do one after another.
A node's ports count as one entity; the path follows scheduling and per-node order, so it is a lower
bound. Replays of four paper runs (identical to SimAI):

| run | critical path (events) | events | events per step | dependent events per 25 ns slot | wall | wall per critical event |
|---|---|---|---|---|---|---|
| GPT-3 13B probe, 128 GPUs | 8.43 M | 656 M | 78 | 3.7 | 89.9 s | 10.7 µs |
| LLaMA 65B probe, 128 GPUs | 4.16 M | 839 M | 202 | 1.3 | 44.0 s | 10.6 µs |
| GPT-3 175B probe, 128 GPUs | 12.5 M | 2759 M | 221 | 1.6 | 136.7 s | 11.0 µs |
| AllReduce 512 MB, 128 GPUs | 1.21 M | 115 M | 95 | 0.5 | 10.2 s | 8.4 µs |

TwiNS's time is about 10 µs per event of the critical path in all four. So it already runs close to
the bound its event latency sets: another way of running the same events in parallel gains little.
Only a lower latency per dependent event can. SimAI, on one core of a87, spends about 0.8 µs per
event (518 s for the 13B run's 656 M events, counted as TwiNS counts them).

**Where the events are.** In the 13B run, 77.7% of the events are inside servers: 258 M at the GPUs'
NVLink devices and 252 M at the NVSwitches, against 37 M at the NICs and 110 M at ToRs and spines.
LLaMA 65B: 82.1%. That is 31.9 M events per server (13B), 43.1 M (LLaMA).

## The servers on their own: an oracle

Flows between the GPUs of one server use only the server's NVLinks and NVSwitch, and flows between
servers never touch them (`FlowInfo::fast`; ports are counted per host pair, so the two kinds never
share one). They meet only in the system layer, through completions and actions, which the closed
loop already carries every send_lat. So a server's NVLink part could run elsewhere, on a CPU core,
exchanging completions with the GPU at each sync.

`twins-ai --nvl-record=F` writes every completion of such a flow with its chain and the tail nodes
the chain reaches (each once, up to 1024 deep). `--nvl-oracle=F` then does not run those flows: it
puts the recorded completions into the ring as the GPU passes them, with their tails rebuilt in the
new run's ring, and NVSwitches no longer force single slots. A comparison that needs a tail deeper
than recorded counts as on a lost tail.

| run | GPU, closed loop | GPU, oracle | against SimAI (messages, DAG lines) |
|---|---|---|---|
| AllReduce 512 MB, 128 GPUs | 11.9 s | 11.5 s | identical (no NVLink flows) |
| GPT-3 13B probe, 128 GPUs | 90.1 s | 14.3 s | identical; 4096 ties reached a cut tail (reported) |
| LLaMA 65B probe, 128 GPUs | 45.2 s | 13.2 s | identical, no lost tails |

So the separation holds, and without the NVLinks the GPU does the 13B run's network in a sixth of the
time. The oracle's own wall time (36 s and 69 s) is mostly copying tens of millions of tail nodes
into the ring, which running the servers elsewhere would not need.

**What running them on CPU cores would take.** `--nvl-record` also writes each server's count of
such events per sync (`F.ev`); `validation/simai/nvl_project.py` spreads the servers over P cores
and charges each window its busiest core (events × c) and a barrier, before the window ends. With
the oracle's GPU time and the system layer and sync on top:

| run, now | cores | c = 0.2 µs | c = 0.4 µs | c = 0.8 µs |
|---|---|---|---|---|
| 13B probe, 89.9 s | 16 | 17–28 s | 25–40 s | 48–62 s |
| 13B probe | 4 | 33–48 s | 64–78 s | 126–140 s |
| LLaMA probe, 49.9 s | 16 | 18–27 s | 22–35 s | 39–52 s |
| LLaMA probe | 4 | 39–52 s | 74–87 s | 143–156 s |

The ranges run from GPU and CPU fully overlapped to one after the other. c is the cost of one event
on a core: SimAI spends 0.8 µs on a87 (Xeon Gold 5218); a 9950X core is about twice as fast, and an
engine without ns-3's overheads faster again, but no CPU version of TwiNS's server model exists to
measure it. So on 16 cores and at 0.4 µs or less, running the servers on the CPU would gain 2.3–3.6×
for the 13B run and 1.4–2.3× for LLaMA. On 4 cores it gains little, or loses.

**c, measured.** `validation/simai/nvl_island_bench.cc` is a sequential event loop with SimAI's
NVLink behaviour (QPs paced at line rate within their window, an ACK per frame, the NVSwitch a FIFO
per egress port, a chain pushed per event), fed the 13B run's recorded flows within servers. It is
not exact, but it does the same work: 503.5 M events against the GPU's 509.9 M, and it ends at
56.372 ms against 56.374. On this machine's i5-7500 it takes 0.141 µs per event on one thread and
0.177 µs with 4 threads (71.1 s and 22.5 s for the 16 servers). An exact engine also compares chains
at ties; at 0.2 µs, the table gives 33–48 s for the 13B run on these 4 cores (1.9–2.7×) and 17–28 s
on 16 (3.2–5.4×).

**Not built yet: the exact engine.** It has to make the same completions, chains included, as the GPU.
The safe way is the same code: rstep.cuh's host and switch steps run with one lane (switches serial,
which is exact) on a CPU backend of the runtime (allocation, the tails' ring, the memo, the slot
loop), with a second Sim in the same process for the servers. That is a port of the runtime, weeks
rather than days. The recording and the oracle are its test: its completions must equal the
recorded ones, chain for chain.

## Rejected, with measurements

- **`--win=32` on GPT-3 13B.** 128 GPUs 97.97 s against 87.95 s; 512 GPUs 252 s against 196 s.
- **NVLink frame trains.** This was the one way past about 25×. Offline, frames that could join a train
  are 4.6% of the time and 11.4% of the bytes on GPT-3 13B probe at 128 GPUs, and 0% on LLaMA 65B and
  GPT-3 175B: below the 50% set as the condition to start. Not built.
- **Lane-0-only hosts.** Patch tested, gate passed, no gain: `c_allreduce_64m` 1.000, LLaMA probe 128 1.004,
  13B probe 128 1.016, HPCC k=16 1.020 (new/old time).
- **Dynamic server islands.** 20 serial slots would have to fit under about 800 µs at 128 GPUs. At 26000
  cycles per event, the estimate is 4–6× over. Not built.
- **Active-set dispatch.** At 512 GPUs, empty entities are about 20% of cycles and the GPU is near its
  concurrency cap, so at most about 1.25×. It risks missing `gk_hz` or `next_wake`, DAG marks and
  `closed_apply`. Not built.
- **CPU and GPU overlapping.** At most about 10% where the GPU dominates. Not built.
- **Fork after init.** Init takes 0.25 s. **`-O3` alone**: about nothing; kept, as it costs nothing.
- **`free_packets` counters alone.** Stub 55 → 52 s. The guess was wrong: a DWARF-stack profile showed the
  real hot spot, the per-channel walk in `StreamInit`.
- **A flows cache kept across runs.** No gain on a first run, and reruns are now rare.

## What went wrong on the way

- **A hang from `2b362e1`.** In a sparse window, a switch could stay marked busy with no frame to come, so
  the network never went idle. Three DCN+ 4 MB runs that strand flows (`ar64`, `llp`, `a2a`) then timed
  out. The gate had no case with stranded flows, and the commit went in before its gate finished; the
  matrix found it. `056db5b` fixes it, and the gate now has `ar64` on DCN+ 4 MB.
- **Replay of GPT-3 175B probe at 1024 GPUs does not fit with the defaults.** It holds 11 M flows and the
  DAG's tables, which leave 390 MB of the card's 16.9 GB for the tails. With the default 16384 messages of DAG room
  it runs out of memory after about 15 s. `--dcap=8192` fits and is identical. The closed loop has no such
  tables and needs no options.

## RTX 5090

The CUDA port (`ai-speed-cuda`) gives identical results to the references: all 8 replay cases and all 8
closed-loop cases tried, including their DAG lines. Its speed could not be measured cleanly: other users'
jobs had the GPU at 100% whenever it was checked, and sync time was inflated. Under that contention, replay was up
to 3.1× faster than the code before these changes on window-heavy cases (`c_allreduce_512m_hpn128` 43.2 →
13.9 s, LLaMA probe at 1024 GPUs 425.7 → 147.9 s) and unchanged on GPT-3 13B. Those figures compare this
work's two builds on a shared GPU; they are not TwiNS against SimAI.
