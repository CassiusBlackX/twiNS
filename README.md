# TwiNS

TwiNS is a GPU packet-level simulator of data-center networks that reproduces **sequential
ns-3 exactly**. Given ns-3's ECMP hashes, every flow's connection-establishment and completion
time equals ns-3's, to the nanosecond.

It models:
- TCP NewReno, DCTCP and Cubic as in ns-3's TcpSocketBase, with ns-3's TcpTxBuffer and
  TcpRxBuffer: any flow size, persistent connections carrying several flows, and closing (FIN);
- RED queues with ECN;
- point-to-point links;
- per-flow ECMP or per-packet spraying (ns-3's random ECMP, its MRG32k3a draws reproduced);
- FatTrees, leaf-spines, or any topology from a file (Jellyfish, tori, Dragonflies, ...), with
  link failures and recoveries.

On one consumer GPU (AMD RX 9060 XT) it is:
- 55–970× faster than sequential ns-3;
- 10–279× faster than 32-thread Unison, with the gap growing with the topology (k=8: 10–30×,
  k=16: 41–83×, k=32: 184–279×);
- 4–13× faster than GeDES, whose simplified model is off by 15–92% in mean FCT.

Because results are exact, a parameter sweep run with TwiNS gives ns-3's conclusions. The k=16
sweep below took 10.4 s, against 1,953 s for ns-3 on 32 cores.

## Build

```shell
cmake -S . -B build -DCMAKE_PREFIX_PATH=/opt/rocm -DCMAKE_HIP_COMPILER=/opt/rocm/llvm/bin/clang++
cmake --build build -j
```

- The default architecture is `gfx1200`. Set another with `-DCMAKE_HIP_ARCHITECTURES=gfx...`.
- `-DTWINS_GPU_BACKEND=CUDA` selects the CUDA build. It has not been tested on NVIDIA GPUs yet.
- The build turns off FMA contraction (`-ffp-contract=off`, `--fmad=false`). Exactness depends
  on it.

## Run

```shell
./build/twins --k=8 --flows=data/flows/k8_paper.csv --out=fct.csv --tcp=dctcp|newreno|cubic \
              [--ecmp_from=data/ns3/ref/k8_paper_TcpDctcp.csv] [--ecnK=30] [--queue=200]
./build/twins --topo=leafspine --leaves=16 --spines=8 --hpl=16 --flows=data/flows/ls_paper.csv ...
./build/twins --topo=graph --topofile=data/topo/jf32.txt --flows=... [--fail=300000:s2:s10,700000:s2:s10:up]
./build/twins --k=8 --flows=... --spray=1 [--RngRun=1]   # per-packet ECMP
./build/twins --k=8 --flows=... --fin=1                  # senders close their connections
./build/twins --k=8 --worlds=worlds.txt   # a batch of independent simulations
```

- **Flow files** are `src,dst,flow_size,start_ns` (all-to-all, incast) or `node,flow_size,start_ns`
  (GeDES's host pairing). `workloads/gen.py` generates both. Every flow gets its own TCP
  connection, unless a fifth column `conn` groups flows: rows with the same `conn` (same hosts,
  later starts) are messages written to one persistent connection at their start times
  (`gen.py --reuse 1`). Flow sizes are any byte counts (`gen.py --align byte`).
- **`--fin=1`**: the sender closes the connection after its last flow (FIN with the last
  segment); the receiving PacketSink closes too (its sockets have ShutdownSend set).
- **`--spray=1`**: per-packet ECMP. Each switch draws its route from ns-3's random variable
  (MRG32k3a, stream = the switch's ns-3 node id, as the scenario assigns; `--RngSeed`,
  `--RngRun` as in ns-3).
- **`--topo=graph --topofile=F`**: any topology (`workloads/topo.py`: FatTree, leaf-spine,
  Jellyfish, torus, Dragonfly). Hosts are single-homed; links are 100 Gbps with 1 µs delay;
  switches have at most 32 ports. Routes are the shortest-path equal-cost sets that ns-3's global
  routing installs (`--dump_routes` prints them in the format of the scenario's `DUMP_ROUTES`).
- **`--fail=T:sA:sB[:up],...`** (topology files): at T ns the first link between switches A and
  B goes down (or up): both interfaces drop what arrives, and routes are recomputed at that
  instant (`Ipv4::SetDown` + `RecomputeRoutingTables` in ns-3).
- **Output** has the columns of the ns-3 scenario: `src,dst,flow_size,start_ns,established_ns,completed_ns`.
  FCT is `completed_ns - established_ns`.
- **`--ecmp_from`** takes an ns-3 output of the same flows. Its `hash_data,hash_ack` columns make
  every flow take ns-3's path. Without it, flows are hashed independently. The statistics are
  the same, but per-flow paths and results differ.
- **`--worlds`**: each line of a worlds file is one simulation, for example
  `flows=a.csv,tcp=cubic,ecnK=40,queue=100,ecmp_from=ref.csv,out=o.csv`. The worlds run side by
  side as disjoint copies of the topology, and each world's output equals its standalone run.
  `bench/sweep.py` builds such batches for parameter grids.
- **Counters:** the last output line reports `tx-cap`, `active-ovf`, `ooo-ovf`, `due-ovf`, `rtt-ovf`
  and `ring-ovf`. If any capacity limit was hit, TwiNS prints a warning on stderr, because
  results may then differ from ns-3.

## How it works

**Execution model.**
- Time advances in slots of one link propagation delay (1 µs), the lookahead: a frame sent in
  slot s reaches the next hop no earlier than slot s + 1.
- Within a slot, every host (one GPU thread) and every switch port processes its own events in
  timestamp order, with picosecond timestamps. Slots synchronize entities without quantizing
  behaviour.
- One kernel runs per slot, and whole idle slots are skipped exactly.

**What exactness takes.** Besides the protocols themselves, matching ns-3 to the nanosecond
required modelling:
- *ns-3's event order.* Simultaneous events run in the order they were scheduled.
  - Every frame and timer carries a run-length-encoded chain of its ancestor events: enqueue,
    TransmitComplete, arrival, timers, ScheduleNow. The chain is rooted at an application start.
  - Ties are resolved by comparing these chains. This replaces ns-3's global event queue, which
    a GPU cannot have.
- *The device queue.* After a packet leaves PointToPointNetDevice's queue, ns-3 refills it from
  a Wake event scheduled for the same instant. So RED's queue length depends on whether an
  enqueue runs before or after that event.
- *Socket details.*
  - `Send` defers SendPendingData by 1 ps.
  - The SYN-ACK leaves from CompleteFork.
  - A retransmitted SYN still yields an RTT sample.
  - NewAck recomputes the RTO, clearing backoff.
  - SYN/SYN-ACK timers back off.
  - EnterRecovery retransmits without moving `m_nextTxSequence`.
- *TcpTxBuffer without SACK.* Reno emulation: DiscardUpTo's head fix, NextSeg, BytesInFlight,
  and IsLost in DISORDER. Also the window increase on leaving CA_LOSS, and plain ACKs for
  out-of-range segments.
- *ECN.*
  - The handshake ACK's CE mark hides one on the first data segment.
  - CE on ACKs changes the sender's state.
  - CWR and CWR_SENT.
  - DCTCP's CE state machine with its prior ACKs.
- *Floating point as ns-3 computes it on x86-64* (`src/nsmath.h`):
  - glibc 2.39's `pow`, as the FMA variant that the dynamic loader selects (Cubic);
  - `Time::GetSeconds()` through Q64.64 fixed point and x87 long double;
  - the FMA contractions of ns-3's `-march=native` build, e.g. DCTCP's
    `alpha = fma(1 - g, alpha, g * F)`.

  `validation/tools/nsmath_check.cu` compares these bit for bit against values printed by ns-3
  (`validation/tools/nsmath_ref.cc`).

About 30 such corrections were found by diffing queue histories down to the first differing
enqueue and then reading the ns-3 source (see `validation/README.md`).

## Results

**Exactness.** Every flow's establishment and completion time equals sequential ns-3's in all
318 runs. Every row covers DCTCP, NewReno and Cubic.

| set | runs |
|---|---|
| FatTree k=8: 6 workloads × 6 start-time variants (5 jittered by 0–10 ns) | 108 |
| FatTree k=16: 4 workloads, up to 50,818 flows | 12 |
| Leaf-spine, 16 leaves × 16 hosts, 8 spines: 4 workloads, up to 25,390 flows | 12 |
| k=8 sweep: ECN threshold K × queue limit × 2 loads | 138 |
| k=16 sweep: K × queue limit × 2 loads | 48 |

For comparison:
- The GeDES model is off by 15–92% in mean FCT on the same workloads.
- ns-3's own run-to-run noise (start times jittered by 0–10 ns) is 0.2–4.6%.
- Multi-threaded Unison is deterministic, but per flow it differs from sequential ns-3:
  cross-partition ties are ordered differently.

**Speed.** The table shows simulation time (the event loop, without set-up):
- TwiNS ran on an RX 9060 XT.
- ns-3 (sequential) and Unison (32 threads) ran on a 32-core Xeon Gold 5218, with the
  indexed-routing patch (`validation/ns3`).
- For k=16, sequential ns-3 was timed with 8 runs sharing the machine.
- GeDES has only the paper workloads, because it handles one connection per host.

| workload | flows | TCP | ns-3 | Unison (32 thr) | GeDES | TwiNS | vs ns-3 | vs Unison |
|---|---|---|---|---|---|---|---|---|
| k8 paper | 128 | DCTCP | 24.8 s | 3.45 s | 1.65 s | 0.224 s | 111× | 15× |
| k8 paper | 128 | NewReno | 40.3 s | 11.0 s | 1.65 s | 0.365 s | 110× | 30× |
| k8 fullsync | 128 | DCTCP | 2.82 s | 0.457 s | | 0.035 s | 81× | 13× |
| k8 fullexp | 128 | DCTCP | 4.09 s | 0.757 s | | 0.075 s | 55× | 10× |
| k8 a2a30 | 273 | DCTCP | 21.5 s | 3.19 s | | 0.326 s | 66× | 10× |
| k8 a2a30 | 273 | NewReno | 30.3 s | 8.27 s | | 0.396 s | 76× | 21× |
| k8 incast16 | 160 | DCTCP | 0.658 s | 0.512 s | | 0.085 s | 8× | 6× |
| k8 a2a50s | 12519 | DCTCP | 55.1 s | 4.97 s | | 0.357 s | 154× | 14× |
| k8 a2a50s | 12519 | NewReno | 55.4 s | 7.81 s | | 0.438 s | 126× | 18× |
| k16 paper | 1024 | DCTCP | 352 s | 23.1 s | 2.51 s | 0.434 s | 811× | 53× |
| k16 paper | 1024 | NewReno | 535 s | 45.9 s | 2.51 s | 0.551 s | 971× | 83× |
| k16 a2a30 | 2253 | DCTCP | 419 s | 25.9 s | | 0.637 s | 658× | 41× |
| k16 a2a30 | 2253 | NewReno | 491 s | 54.6 s | | 0.833 s | 590× | 66× |
| k16 incast16 | 160 | DCTCP | 0.913 s | 1.90 s | | 0.076 s | 12× | 25× |
| k16 a2a50s | 50818 | DCTCP | 486 s | 31.6 s | | 0.507 s | 958× | 62× |
| k16 a2a50s | 50818 | NewReno | 444 s | 61.3 s | | 1.050 s | 423× | 58× |

At k=32 (8,192 hosts), DCTCP:
- TwiNS takes 1.2–1.7 s per workload.
- GeDES takes 15.9 s on the paper workload.
- Before its event loop starts, ns-3 spends 2–2.6 hours on routing set-up alone.

| workload | flows | Unison (32 thr) | TwiNS | speedup |
|---|---|---|---|---|
| k32 paper | 8192 | 220 s | 1.20 s | 184× |
| k32 a2a50s | 162505 | 337 s | 1.21 s | 279× |
| k32 incast16 | 160 | 16.3 s | 0.073 s | 223× |

**Parameter sweeps.** `bench/sweep.py` runs a grid as batches of worlds;
`bench/sweep_report.py` tabulates short-flow p99 and long-flow mean FCT per configuration.

The k=8 grid has 138 configurations:
- ECN/RED threshold K ∈ {5, 10, 20, 30, 40, 60, 80, 120};
- queue limit ∈ {100, 200, 400}, keeping K < queue limit;
- NewReno, DCTCP and Cubic;
- websearch all-to-all at 30% and 50% load.

The k=16 grid (1,024 hosts) has 48 configurations: K ∈ {10, 20, 40, 80} × queue ∈ {100, 400} ×
3 TCPs × the same two loads. All outputs of both grids are identical to ns-3's.

| sweep | ns-3, sum of runs | ns-3 on 32 cores | TwiNS, one at a time | TwiNS, batched |
|---|---|---|---|---|
| k=8, 138 configurations | 11,142 s | 455.7 s | 76 s | 4.4 s (69 worlds; 104×) |
| k=16, 48 configurations | 39,641 s | 1,953.5 s | 48.7 s | 10.4 s (16 worlds; 188×) |

The speedups in parentheses are against ns-3 on 32 cores.

Short-flow tails are far from smooth in K. For example, with DCTCP at k=8, 30% load and a
100-packet queue, p99 goes 118, 114, 91, 175, 127, 1120 and 523 µs for K = 5 to 80. The cause is
whether more than 1% of short flows hit the 1 ms minimum RTO. That is why approximate models are
risky for such tuning.

## Reproducing

```shell
validation/check_exact.sh "k8_paper k8_a2a30 ls_paper k16_a2a50s" "dctcp newreno cubic" [jit|jit1..jit4]
validation/check_sweeps.sh            # both sweeps against data/ns3/sweep_k8, sweep_k16
python3 validation/noise.py           # TwiNS and the GeDES model next to ns-3's noise (needs numpy)
bench/speed.sh 3 k8_paper k16_a2a50s  # wall time, min of 3 runs
```

The stored ns-3 outputs in `data/ns3` make these checks independent of ns-3. To regenerate
them, see `validation/README.md`.

## Layout

| path | contents |
|---|---|
| `src/` | the simulator: `model.h` (state and execution model), `step.cuh` (device code), `main.cu` (driver), `nsmath.h` (ns-3's floating point) |
| `workloads/gen.py` | flow-file generator: websearch/exponential sizes; pair, all-to-all and incast patterns |
| `validation/` | exactness checks, the ns-3 scenario and patch, ns-3 run scripts, analysis tools |
| `bench/` | speed measurement and parameter sweeps |
| `data/flows/` | the flow files of all runs above (`*_jit*`: starts jittered by 0–10 ns; `*.topo`: leaf-spine parameters) |
| `data/ns3/ref/` | sequential ns-3 outputs: k=8 (all variants), k=16, leaf-spine; 3 TCPs |
| `data/ns3/sweep_k8/`, `sweep_k16/` | ns-3 outputs of the sweeps, `{workload}_{Tcp}_q{queue}_K{K}.csv` |
| `data/gedes/` | GeDES-model outputs on the k=8 workloads, for `validation/noise.py` |
| `data/twins_speed.csv` | TwiNS wall times behind the speed table |

## Limits

- **Topology size.** The message buffers are sized for the worst case, 300 frames of 64 bytes
  per link and slot. This caps the topology at k=32 on a 16 GB GPU, and switch blocks assume at
  most 32 ports.
- **Batch size.** Batches are limited by GPU memory: a 16 GB GPU holds 69 k=8 worlds or 16 k=16
  worlds.
- **Topology files.** Links all run at 100 Gbps with 1 µs delay (the slot is the propagation
  delay; mixed rates are not modelled yet); hosts have one link.
- **Protocols.** Not yet modelled: RDMA with PFC (DCQCN, HPCC).
- **What exactness is relative to.** It is defined against sequential ns-3.46.1 built with GCC
  `-march=native` on x86-64 (glibc 2.39). Other ns-3 versions or build flags may need
  realignment, which the queue-history diff makes mechanical.
