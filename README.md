# TwiNS

TwiNS is a GPU packet-level simulator of data-center networks that reproduces **sequential
ns-3 exactly**. Every flow's connection-establishment and completion time equals ns-3's, to the
nanosecond. TwiNS computes ns-3's ECMP flow hashes itself, so no ns-3 run is needed.

It models:
- TCP NewReno, DCTCP and Cubic as in ns-3's TcpSocketBase, with ns-3's TcpTxBuffer and
  TcpRxBuffer: any flow size, persistent connections carrying several flows, and closing (FIN);
- RED queues with ECN;
- point-to-point links;
- per-flow ECMP or per-packet spraying (ns-3's random ECMP, its MRG32k3a draws reproduced);
- FatTrees, leaf-spines, or any topology from a file (Jellyfish, tori, Dragonflies, ...), with
  link failures and recoveries.

On one consumer GPU (AMD RX 9060 XT) it is:
- 7–790× faster than sequential ns-3 (39–790× on all but the 160-flow incasts);
- 5–236× faster than 32-thread Unison, with the gap growing with the topology (k=8: 5–24×,
  k=16: 19–62×, k=32: 153–236×);
- 3–11× faster than GeDES, whose simplified model is off by 15–92% in mean FCT.

Because results are exact, a parameter sweep run with TwiNS gives ns-3's conclusions. The k=16
sweep below took 16 s, against 1,953 s for ns-3 on 32 cores.

(Runs that need none of the general features, one message per connection, no FIN, per-flow ECMP
on a FatTree or leaf-spine, use a lean kernel; the general model is 1.2–1.5× slower than it.)

**RDMA.** A second program, `twins-rdma`, reproduces the HPCC simulator exactly. The HPCC
simulator is the ns-3 fork that RDMA studies build on (ns3-rdma, ConWeave). twins-rdma models:
- the RDMA NIC's queue pairs: rate pacing, windows, go-back-N;
- PFC with the shared-buffer MMU's dynamic thresholds and headroom;
- ECN marking at dequeue;
- DCQCN and HPCC with INT.

In 47 validation runs every flow's completion time equals the reference's. The runs span FatTree
k=4–16, leaf-spine, Jellyfish, torus and Dragonfly, PFC storms, and every switch of the config.
twins-rdma is 3.5–200× faster than the reference (25–200× on the larger workloads, 180–200× at
k=16; incasts, whose hot switch port serializes everything, 4–8×). See [RDMA](#rdma).

**AI workloads.** `twins-ai` runs astra-sim's real system layer on the CPU and twins-rdma's network on
the GPU, with no SimAI run first. Against SimAI, every message and every action of the system layer
is identical on 5 workloads across Spectrum-X, AlibabaHPN, DCN+, HPCC, 200G NICs and small buffers,
and on 41 of the 42 runs of SimAI's paper workloads, up to 1024 GPUs. It is 3–18× faster than SimAI on
one core. `src/astra/fetch.sh` gets SimAI's code first. See [docs/ai/whatif.md](docs/ai/whatif.md).

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
              [--ecnK=30] [--queue=200]
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
- **ECMP hashes.** Per-flow ECMP picks a route by ns-3's flow hash: `Hash32` (Murmur3) of the
  connection's source and destination IP, protocol and ports, in each direction. TwiNS
  computes them as ns-3 assigns these values in the reference scenario:
  - a host's IP is `10.(n>>8).(n&255).1`, with `n` its link's subnet;
  - the sink's port is 1000;
  - each sender binds the host's next ephemeral port, from 49153, in order of start time, then
    of connection.

  `validation/check_hashes.sh` compares them with every stored ns-3 output: all 292 match.
  `--ecmp_from=csv` takes the hashes from an ns-3 output of the same flows instead (its
  `hash_data,hash_ack` columns), and `--hash_check=1` only compares the two and exits.
- **`--worlds`**: each line of a worlds file is one simulation, for example
  `flows=a.csv,tcp=cubic,ecnK=40,queue=100,out=o.csv` (`ecmp_from=` is optional). The worlds run side by
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
477 runs. Every row covers DCTCP, NewReno and Cubic.

| set | runs |
|---|---|
| FatTree k=8: 6 workloads × 6 start-time variants (5 jittered by 0–10 ns) | 108 |
| FatTree k=16: 4 workloads, up to 50,818 flows | 12 |
| Leaf-spine, 16 leaves × 16 hosts, 8 spines: 4 workloads, up to 25,390 flows | 12 |
| Flow sizes that are not MSS multiples: k=8 (3 workloads), leaf-spine, k=16 | 15 |
| Persistent connections carrying several flows (up to 25,344 over 256 connections), with and without FIN, and FIN on 4 earlier workloads | 48 |
| Per-packet spraying: k=8 (5 workloads), leaf-spine, k=16, reuse with FIN | 24 |
| Topology files: Jellyfish (128 and 1,024 hosts), 2-D torus, Dragonfly; per-flow ECMP and spraying | 48 |
| Link failures and recoveries on FatTree, Jellyfish, torus; per-flow ECMP and spraying | 24 |
| k=8 sweep: ECN threshold K × queue limit × 2 loads | 138 |
| k=16 sweep: K × queue limit × 2 loads | 48 |

A few runs (3 of 138 k=8 and 8 of 48 k=16 sweep configurations, k16 a2a30 NewReno with spraying)
contain simultaneous events whose recorded histories are equal over all 9 runs a 64-byte frame
holds. TwiNS counts such undecided ties and warns; `twins128` (128-byte frames, 25 runs) decides
them, and the validation scripts and `bench/sweep.py` rerun with it when needed.

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
| k8 paper | 128 | DCTCP | 24.8 s | 3.45 s | 1.65 s | 0.296 s | 84× | 12× |
| k8 paper | 128 | NewReno | 40.3 s | 11.0 s | 1.65 s | 0.467 s | 86× | 24× |
| k8 fullsync | 128 | DCTCP | 2.82 s | 0.457 s | | 0.045 s | 63× | 10× |
| k8 fullexp | 128 | DCTCP | 4.09 s | 0.757 s | | 0.105 s | 39× | 7× |
| k8 a2a30 | 273 | DCTCP | 21.5 s | 3.19 s | | 0.442 s | 49× | 7× |
| k8 a2a30 | 273 | NewReno | 30.3 s | 8.27 s | | 0.501 s | 60× | 17× |
| k8 incast16 | 160 | DCTCP | 0.658 s | 0.512 s | | 0.099 s | 7× | 5× |
| k8 a2a50s | 12519 | DCTCP | 55.1 s | 4.97 s | | 0.432 s | 128× | 12× |
| k8 a2a50s | 12519 | NewReno | 55.4 s | 7.81 s | | 0.555 s | 100× | 14× |
| k16 paper | 1024 | DCTCP | 352 s | 23.1 s | 2.51 s | 0.555 s | 634× | 42× |
| k16 paper | 1024 | NewReno | 535 s | 45.9 s | 2.51 s | 0.746 s | 718× | 62× |
| k16 a2a30 | 2253 | DCTCP | 419 s | 25.9 s | | 0.899 s | 466× | 29× |
| k16 a2a30 | 2253 | NewReno | 491 s | 54.6 s | | 1.15 s | 428× | 48× |
| k16 incast16 | 160 | DCTCP | 0.913 s | 1.90 s | | 0.099 s | 9× | 19× |
| k16 a2a50s | 50818 | DCTCP | 486 s | 31.6 s | | 0.616 s | 790× | 51× |
| k16 a2a50s | 50818 | NewReno | 444 s | 61.3 s | | 1.31 s | 339× | 47× |

At k=32 (8,192 hosts), DCTCP:
- TwiNS takes 0.1–1.4 s per workload.
- GeDES takes 15.9 s on the paper workload.
- Before its event loop starts, ns-3 spends 2–2.6 hours on routing set-up alone.

| workload | flows | Unison (32 thr) | TwiNS | speedup |
|---|---|---|---|---|
| k32 paper | 8192 | 220 s | 1.43 s | 153× |
| k32 a2a50s | 162505 | 337 s | 1.43 s | 236× |
| k32 incast16 | 160 | 16.3 s | 0.097 s | 169× |

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
| k=8, 138 configurations | 11,142 s | 455.7 s | 100 s | 6.2 s (69 worlds; 74×) |
| k=16, 48 configurations | 39,641 s | 1,953.5 s | 79 s | 16.0 s (16 worlds; 122×) |

The speedups in parentheses are against ns-3 on 32 cores. The batched times include rerunning
the worlds with undecided ties with `twins128` (3 at k=8, 8 at k=16).

Short-flow tails are far from smooth in K. For example, with DCTCP at k=8, 30% load and a
100-packet queue, p99 goes 118, 114, 91, 175, 127, 1120 and 523 µs for K = 5 to 80. The cause is
whether more than 1% of short flows hit the 1 ms minimum RTO. That is why approximate models are
risky for such tuning.

## RDMA

```shell
build/twins-rdma validation/hpcc/hpcc.cfg --topo=data/topo/fattree8.txt --flows=data/flows/k8_a2a30.csv \
    --out=fct.txt [--txtrace=tx.txt] [CC_MODE=3 BUFFER_SIZE=2 KMAX_MAP=1,100000000000,800 ...]
```

- **Parameters.** twins-rdma reads the HPCC simulator's config files (`mix/config.txt`).
  `KEY=VALUE` overrides a key; use `KEY=V1,V2,...` for keys with several values.
- **Inputs.** The same topology files (`workloads/topo.py`) and flow files (`src,dst,flow_size,start_ns`)
  as twins.
- **Output.** One line per flow: `id start_ns fct_ns`.

**Batches.** `--batch=JOBFILE --outdir=DIR` runs every line of a job file
(`name topo flows [KEY=VALUE ...]`) as a world of its own, side by side in one GPU run, and
writes `DIR/name.fct`. The worlds may differ in topology, flows and every config key except the
link rate and delay. A single run fills only a few hundred waves, so batches multiply throughput.

The k=8 sweep in `validation/hpcc/sweep_k8.txt` has 32 configurations: 2 workloads, DCQCN and
HPCC, 2 buffer sizes, 4 ECN thresholds. As one batch it takes 3.2 s, and every world matches
its reference. The same 32 runs take 19.0 s one after another. The reference takes 1,783 s on one
core and 63.1 s on 32 cores of the Xeon (32 processes at once): 554× and 20×.

The reference is the HPCC fork's own scenario on these files. Two changes make it reproducible:
- routes are ordered by node id instead of by heap address;
- each switch port draws its ECN marks from its own random stream.

The unpatched fork creates a new random variable per draw, whose stream depends on every
variable created before it. `validation/hpcc/README.md` covers:
- the reference and its container (the fork needs gcc 5);
- the job files and the comparison tools;
- what is not modelled: TIMELY, DCTCP-over-RDMA, HPCC-PINT, per-hop rates, link failures, mixed
  link rates.

**How it differs from the TCP model.**
- **Switches.** A switch's MMU couples its ports, since every PFC threshold depends on the
  switch-wide shared buffer. Each switch therefore runs in a wave of its own. Its lanes first load
  the slot's arriving frames together, with their routes, into a pool in global memory. Only
  small state stays in LDS: ports, cursors, chain prefixes, and 16-bit arrival times and orders.
  LDS bounds how many switches a CU holds, and at k=16 that was the limit.
  - **Safe slots** are the common case: no port is MMU-paused, none has headroom bytes, and every
    port stays below the lowest threshold the slot can reach even if all its arrivals stay. Then
    no MMU decision can occur. Each lane runs one egress port's events: the arrivals routed to it
    and its TransmitCompletes, in their order. Ingress counters are summed with LDS atomics, and
    shared_used follows at the end from `Σ max(0, ing − reserve)`.
  - **Otherwise** (PFC near) one lane runs all the switch's events in order.
- **Hosts.** A host's lanes load the slot's arrivals and compute HPCC's per-hop utilizations of
  its ACKs ahead. With fast react on, an ACK's utilizations depend only on that ACK and on the
  QP's previous ACK, which leaves one lane five divisions per ACK instead of about 25.
- **Chains.**
  - Chains compare leads before ordinals. Ordinals order only the events one parent schedules,
    and comparing them across parents was wrong.
  - Chains are not truncated. Past 12 runs, the older runs spill into a shared ring of immutable
    nodes, which a comparison follows when the recent runs tie. The validation runs have no
    undecided ties.

| run | flows | reference | twins-rdma | speedup |
|---|---|---|---|---|
| k16 a2a30, DCQCN | 2253 | 199.8 s | 0.99 s | 202× |
| k16 a2a30, HPCC | 2253 | 215.9 s | 1.14 s | 189× |
| k16 a2a50s, DCQCN, 2 MB buffers (4043 pauses) | 50818 | 210.0 s | 1.17 s | 180× |
| k8 a2a50s, HPCC | 12519 | 35.3 s | 0.44 s | 79× |
| k8 permutation, HPCC | 382 | 10.6 s | 0.094 s | 113× |
| leaf-spine a2a30, DCQCN | 541 | 30.4 s | 0.63 s | 48× |
| Jellyfish a2a30, HPCC | 289 | 20.1 s | 0.76 s | 26× |
| k8 incast64, 1 MB buffers (4315 pauses), DCQCN | 64 | 4.1 s | 0.92 s | 4.5× |

These are wall times on the RX 9060 XT against the reference on one core of a Xeon Gold 5218.
The reference times include the start of its container (about 0.4 s).

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
| `src/rdma/` | twins-rdma: `rdma.h` (state and execution model), `rstep.cuh` (NIC, switch, slot kernel), `rmain.cu` (driver) |
| `src/astra/` | twins-ai: `frontend.cc` (astra-sim over a network), `closed.h` (the loop with the GPU), `fetch.sh` (SimAI's code) |
| `validation/hpcc/` | the RDMA reference (`rdma-ref.cc`, `hpcc.patch`, container), job files, `check.sh`, `txdiff.py`, `events.py` |
| `data/rdma/` | topologies and flow files of the RDMA runs |

## Limits

- **Topology size.** The message buffers are sized for the worst case, 300 frames of 64 bytes
  per link and slot. This caps the topology at k=32 on a 16 GB GPU, and switch blocks assume at
  most 32 ports.
- **Batch size.** Batches are limited by GPU memory: a 16 GB GPU holds 69 k=8 worlds or 16 k=16
  worlds (`twins128`: about half).
- **Speed.** Two kernels: a lean one (one message per connection, no FIN, per-flow ECMP on a
  FatTree or leaf-spine: a compact sent list, a bitmap receiver) and the general one (the
  TcpTxBuffer as a ring of items, merged and split as ns-3 does; FIN; spraying; topology files),
  which is 1.2–1.5× slower. The lean kernel is within 1.1–1.45× of the first version, which
  modelled only that case.
- **Event-order ties.** When two simultaneous events have equal histories beyond what a frame
  records, the order is a guess; TwiNS reports these (`undecided-ties`) and `twins128` reaches
  further back.
- **Topology files.** Links all run at 100 Gbps with 1 µs delay (the slot is the propagation
  delay; mixed rates are not modelled yet); hosts have one link.
- **Protocols.** RDMA (PFC, DCQCN, HPCC) is modelled by `twins-rdma`, against the HPCC
  simulator rather than ns-3.46; see `validation/hpcc/README.md` for what it does not cover.
- **What exactness is relative to.** It is defined against sequential ns-3.46.1 built with GCC
  `-march=native` on x86-64 (glibc 2.39). Other ns-3 versions or build flags may need
  realignment, which the queue-history diff makes mechanical.
