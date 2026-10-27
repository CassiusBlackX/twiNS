# Phase 0 baseline: SimAI's speed and reproducibility

**Setup.**
- SimAI `a57d765` on a87 (64 cores), in an Ubuntu 22.04 container with gcc 11.
- Workload: `example/microAllReduce.txt`, two all-reduces (16 MB, 64 MB) with 556 µs of compute
  before each.
  - For 16 and 128 GPUs, the tensor-parallel group is all GPUs, and NCCL rings span the servers.
  - `AS_SEND_LAT=3`.
  - Topologies from `gen_Topo_Template.py`:
    - Spectrum-X: H100, 400 Gb/s;
    - AlibabaHPN: H100, 200 Gb/s, dual ToR;
    - DCN+: A100, 200 Gb/s, single ToR.
  - NVLink: 2880 Gb/s (2400 for A100) at 25 ns.
  - NIC links: 500 ns.
- Runs were not isolated: up to 14 ran at a time on the 64 cores. Phase 4 repeats the timings
  alone.

**Builds.**
- `debug`: SimAI's own build (`-d debug --enable-mtp`).
- `mtp`: the same, optimized.
- `seq`: optimized, without MTP (`simai-seq.patch`).

## Reproducibility

**Stock SimAI** (MTP, as built by `scripts/build.sh`), same input twice:

| run | iteration time | flows finishing at different times |
|---|---|---|
| DCN+ 16, `-t 1` | 1966.1 / 1951.8 µs | 5394 of 7680 |
| HPN 16, `-t 1` | 1958.4 / 1888.6 µs (1992.8 with `-t 16`) | — |
| Spectrum-X 128, `-t 1` | 3250.7 / 3269.2 µs (3235.7 with `-t 16`) | — |
| DCN+ 128, `-t 1` | 3532.6 / 3636.1 µs | — |

`-t 16` at 128 GPUs never completed a run correctly:

| run | outcome |
|---|---|
| DCN+, debug build | `double free or corruption` after 235 s |
| Spectrum-X, optimized | `double free or corruption` after 42 s |
| HPN, optimized | `double free or corruption` after 42 s |
| HPN, debug build | stopped after 275 s with 274972 of 520192 flows, exit code 0 |
| DCN+, optimized | stopped with 519526 of 520192 flows, exit code 0 |

**Sequential SimAI** (`seq`): repeated runs are byte-identical, in every flow's completion and in
the iteration time.
- Runs checked: Spectrum-X 16 and 128, HPN 16, DCN+ 16.
- Adding per-port ECN streams (`simai-ref`) changes none of these results.

| | Spectrum-X | HPN | DCN+ |
|---|---|---|---|
| 16 GPUs | 1658.8 µs | 2001.0 µs | 1952.7 µs |
| 128 GPUs | 3298.1 µs | 3568.4 µs | 3747.2 µs |

## Speed (wall time, s)

| run | flows | debug `-t 1` | debug `-t 16` | mtp `-t 1` | mtp `-t 16` | seq |
|---|---|---|---|---|---|---|
| Spectrum-X 8 (NVLS) | 128 | 7.0 | — | — | — | — |
| Spectrum-X 16 | 7680 | 23.4 | 29.2 | 5.2 | 4.2 | 4.1 |
| HPN 16 | 7680 | 55.4 | 43.4 | 7.5 | 6.4 | 5.7 |
| DCN+ 16 | 7680 | 18.5 | 20.6 | 3.2 | 2.5 | 2.3 |
| Spectrum-X 128 | 520192 | 557–584 | 311 | — | crash | 75 |
| HPN 128 | 520192 | 578 | (incomplete) | — | crash | 84 |
| DCN+ 128 | 520192 | 528–531 | crash | — | (incomplete) | 70 |

**Observations.**
- The sequential optimized build is the fastest correct configuration: 7–8× faster than the build
  SimAI ships.
- Threads gain little, at most 1.3× with `-t 16` on 16 GPUs.
- At 128 GPUs, threads crash or lose flows.

So the speed baseline for TwiNS is `seq`, and stock `-t 16` where it completes.

## TwiNS: what to expect

The cost at Spectrum-X 128 GPUs:
- 520192 flows of 16–64 KB, 21.3 GB in all;
- 2.60 M data frames at MTU 9000, each with an ACK (`L2_ACK_INTERVAL 1`);
- 2–4 links per frame (NVSwitch or ToR, with spine paths across segments), so about 15 M frame
  transmissions;
- 3.3 ms simulated, of which 1.1 ms is compute with no traffic, which idle skipping passes over.

twins-rdma's throughput on the RX 9060 XT (k8 FatTree, 128 hosts, 128 long flows at line rate):

| MTU | flow size | simulated | wall | rate |
|---|---|---|---|---|
| 1000 | 16 MB | 6.41 ms | 1.07 s | about 23 M frames/s |
| 4096 | 16 MB | 5.71 ms | 0.42 s | — |

**The NVLink delay sets the slot** (25 ns) unless a server's GPUs and NVSwitch form one entity.
- With 25 ns slots: 3.3 ms is 0.13 M slots. At the current 5–10 µs per slot that is 1–2 s of slot
  overhead.
- With server entities: the slot is the 500 ns NIC link, 6.6 k slots.

**Estimate.** TwiNS should run Spectrum-X 128 in about 1–3 s against 75 s for `seq`, a speedup of
about 25–75×.
- The GPU's advantage grows with scale and parallel work. That points to 1024+ GPUs, batches of
  configurations, and steady-state collectives with many flows in flight.
- A sweep of 32 configurations in one batch, as with `sweep_k8`, multiplies the gap.
- Phase 4 measures this. Phase 1 builds server entities only if 25 ns slots dominate.
