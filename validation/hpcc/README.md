# Validation of the RDMA model against the HPCC simulator

`twins-rdma` (src/rdma) models the RDMA network of the HPCC simulator
([alibaba-edu/High-Precision-Congestion-Control](https://github.com/alibaba-edu/High-Precision-Congestion-Control),
an ns-3.17/3.18 fork that ns3-rdma, ConWeave and others descend from). Every model below is
the fork's code as it stands:
- **NIC (`QbbNetDevice`, `RdmaEgressQueue`, `RdmaHw`):**
  - queue pairs scheduled round robin;
  - each queue pair paced by its rate and held back by its window;
  - go-back-N with NACKs;
  - ACKs in a high-priority queue.
- **Switch (`SwitchNode`, `SwitchMmu`, `BEgressQueue`):**
  - a shared-buffer MMU with PFC (dynamic thresholds, headroom, resume offset);
  - ECN marking at dequeue (Kmin, Kmax, Pmax);
  - INT for HPCC;
  - per-flow ECMP (`EcmpHash`, seeded by the switch's id).
- **Congestion control:**
  - DCQCN as Mellanox implements it (`CC_MODE 1`: alpha, rate decrease and rate increase timers);
  - HPCC (`CC_MODE 3`, one rate per path: `MULTI_RATE 0`).

## The reference

`rdma-ref.cc` is the fork's scenario (`simulation/scratch/third.cc`) on TwiNS's topology and
flow files. It reads the same HPCC config file as twins-rdma, and `KEY=VALUE` arguments
override single keys (`KEY=V1,V2,...` for keys with several values).

It is reproducible:
- **Routes:** the topology maps are ordered by node id, not by heap address (`Ptr<Node>`).
- **ECN:** marking draws come from one random stream per switch port (`hpcc.patch`).
  Unpatched, every draw creates a new `UniformVariable`, whose stream depends on how many were
  created before it anywhere in the run.
- **Flow starts:** at integer nanoseconds.

`hpcc.patch` also adds a debug trace of one switch's MMU (`TRACE_MMU=node,from_ns,until_ns`,
printed to stderr).

**Setting up.** The fork builds with gcc 5 and python 2, which `Containerfile` provides.

```shell
git clone https://github.com/alibaba-edu/High-Precision-Congestion-Control ~/netsim/hpcc
cd ~/netsim/hpcc && git apply .../validation/hpcc/hpcc.patch
cp .../validation/hpcc/rdma-ref.cc simulation/scratch/
podman build -t hpcc-build -f .../validation/hpcc/Containerfile .
.../validation/hpcc/hpcc.sh "./waf configure --build-profile=optimized && ./waf build"
```

The build uses `-O3 -march=native`, so GCC contracts some floating-point expressions to FMA.
twins-rdma writes these with `fma()`:
- DCQCN's alpha update;
- HPCC's utilization EWMA.

These are the only contractions in `rdma-hw.o`; they were found by disassembling it.

**Running.** `run_jobs.sh JOBFILE [parallel]` runs the jobs of a job file in the container. Each
line of a job file is `name topo flows [KEY=VALUE ...]`, with the files in `$WORK`. It writes
`name.fct` (`id start_ns fct_ns`), `name.tx` (the transmission trace) and `name.log`.

## TwiNS-RDMA against the reference: identical

`check.sh JOBFILE [--tx]` reruns a job file with twins-rdma and compares every flow's start and
completion time. The references come from `out/rdma/ref`. With `--tx` it also prints the first
difference between the two transmission traces (`txdiff.py`).

| job file | runs | contents | identical |
|---|---|---|---|
| `jobs1.txt` | 10 | FatTree k=4 and k=8: incasts, all-to-all (up to 12519 flows); PFC with 1–2 MB buffers | all |
| `jobs2.txt` | 29 | FatTree k=16 (1024 hosts), leaf-spine 16×8, Jellyfish, torus, Dragonfly; incast of 64 with 1 MB buffers (4315 pauses); low ECN thresholds (28k marks); every switch of the config (ACK priority, windows, fast react, MI threshold, target clamping, rate bound, MTU 1500, ACK interval, DCQCN gains and timers) | all |
| `jobs3.txt` | 8 | Safe and serial switch slots alternating within a run: all-to-all and permutation traffic with 1–2 MB buffers (up to 26848 pauses), FatTree k=8 and k=16, leaf-spine; HPCC with `SAMPLE_FEEDBACK` (its serial paths), with and without fast react | all |

Both congestion controls run in each set. HPCC runs only where paths have at most 5 switches.
Beyond that its INT header overflows and the fork writes past the end of `hp.hop[]`.

Ties the chains do not decide, and ties decided on an overwritten tail node, are counted and
reported. A larger `--tail` makes the ring longer. twins-rdma also warns about late data:
data reaching a receiver after it already holds the flow's last byte (see below). Neither
occurs in the 47 runs.

**Finding differences.**
1. `txdiff.py REF SIM` shows the first transmission that differs.
2. `events.py TOPO TRACE NODE FROM UNTIL` lists one node's arrivals, starts and ends from a trace.
3. A build with `-DRDMA_TRACE_NODE=n -DRDMA_TRACE_FROM=ns -DRDMA_TRACE_UNTIL=ns` prints the
   events that node runs, with their chains and its MMU updates. `TRACE_MMU` prints the
   reference's MMU updates for comparison.
4. `-DRDMA_TRACE_TIES` prints ties the chains do not decide.

**Not modelled** (twins-rdma refuses these configs):
- TIMELY, DCTCP-over-RDMA and HPCC-PINT (`CC_MODE` 7, 8, 10);
- HPCC's per-hop rates (`MULTI_RATE 1`);
- `L2_BACK_TO_ZERO`;
- `GLOBAL_T 0`;
- `LINK_DOWN`;
- links with different rates.

Data that reaches a receiver after its queue pair has completed is not modelled either. ns-3
deletes the receiving queue pair at completion and would answer such data with a NACK; this
can only happen after a drop.
