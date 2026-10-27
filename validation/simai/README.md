# Validation against SimAI's ns-3 network

SimAI ([aliyun/SimAI](https://github.com/aliyun/SimAI), NSDI '25) simulates AI training on
three pieces:
- astra-sim-alibabacloud (the system layer);
- SimCCL's NCCL mock (collectives broken into flows);
- ns-3-alibabacloud, the HPCC RDMA model ported to ns-3.36.1, with NVSwitches, GPU servers and
  per-link rates.

TwiNS models the network part of it. `docs/ai/simai-diff.md` lists what that adds to the HPCC
fork.

## Stock SimAI is not reproducible

SimAI's build turns on Unison (`--enable-mtp`), and the simulator enables it even with `-t 1`.
- Unison runs its logical processes in an order sorted by measured execution time.
- astra-sim's state and ns-3's ECN random streams are shared between them.

So the same input gives different results from run to run. Repeated runs of `microAllReduce`,
as a ring all-reduce over 16 GPUs:

| topology | run 1 | run 2 | `-t 16` |
|---|---|---|---|
| AlibabaHPN, 16 GPUs | 1958.4 µs | 1888.6 µs | 1992.8 µs |
| DCN+, 16 GPUs | 1966.1 µs | 1951.8 µs | 1955.5 µs |
| Spectrum-X, 16 GPUs | 1670.3 µs | 1670.3 µs | 1665.0 µs |

In the DCN+ pair, 5394 of 7680 flows finish at different times. With `-t 16`, DCN+ at 128 GPUs
aborted after 235 s with `double free or corruption`.

## The reference: sequential SimAI

`simai-seq` is SimAI built without MTP, so it runs on ns-3's sequential `DefaultSimulatorImpl`.
It needs `simai-seq.patch`: astra-sim initialises its NCCL mock only under MTP or MPI, and
without it the run crashes.
- Repeated runs are byte-identical, in flow completion times and iteration time.
- It is also faster than stock SimAI with `-t 1` (`docs/ai/baseline.md`).

`simai-ref` adds `simai.patch` (ns-3-alibabacloud):
- **ECN**: each switch egress port draws its ECN marks from its own random stream, as
  `validation/hpcc/hpcc.patch` does for the HPCC fork. The draws then no longer depend on how
  many draws happened before anywhere in the network, which is what lets twins-rdma reproduce
  them.
- **Two crashes fixed.** Both change only runs that crash in stock SimAI.
  - **PFC.** `QbbNetDevice::Resume` asks `m_rdmaEQ->GetQp(qIndex)` whether NVLS is on before it
    checks the node type. A switch port has no `m_rdmaEQ`, so the first RESUME frame a switch
    receives crashes SimAI; a host may have fewer than `qIndex + 1` queue pairs. The patch checks
    for an NVSwitch first.
  - **HPCC.** The switch writes INT at `ppp + 20 + 8 + 6`, the HPCC fork's offset, but SimAI's SeqTs
    header is 10 bytes. INT overwrites the data frame's sequence number and priority, and the next
    switch enqueues the frame into queue 168. The patch writes INT at `+ 10`.

`simai-ref.cc` is the network reference without astra-sim.
- It is SimAI's network setup (`common.h`: `ReadConf`, `SetConfig`, `SetupNetwork`, `CalculateRoute`) with the topology maps ordered by node id.
- It reads SimAI's topology files and a flow file, and writes:
  - `id start fct` per flow at the sender's `QpComplete`;
  - `id ns` at its first `SendComplete`;
  - optionally the transmission trace, in the format of `validation/hpcc`.

## Setting up (on a87, under `~/netsim`)

```shell
git clone --recursive https://github.com/aliyun/SimAI simai        # a57d765
podman build -t simai-build -f .../validation/simai/Containerfile .
cp .../validation/simai/simai.sh podman/
podman/simai.sh "./scripts/build.sh -c ns3"                          # stock: debug, MTP
for v in seq ref; do cp -a simai simai-$v; done                      # then in each copy:
git apply .../simai-seq.patch                                        # astra-sim sources (repo root)
#   and copy the two patched files into
#   astra-sim-alibabacloud/extern/network_backend/ns3-interface/simulation/src/applications/astra-sim/
(cd ns-3-alibabacloud && git apply .../simai.patch)                  # simai-ref only; copy
#   switch-mmu.{cc,h} into .../ns3-interface/simulation/src/point-to-point/model/ as well
cp .../simai-ref.cc astra-sim-alibabacloud/extern/network_backend/ns3-interface/simulation/scratch/
SIMAI_DIR=~/netsim/simai-ref podman/simai.sh "cd /simai/astra-sim-alibabacloud/extern/network_backend/ns3-interface/simulation && ./ns3 configure -d optimized && ./ns3 build AstraSimNetwork simai-ref"
```

**Running.** `run_simai.sh NAME TOPO WORKLOAD THREADS [ENV=VALUE ...]` runs SimAI in its own
container, with outputs in `~/netsim/aiwork/runs/NAME`.
- `V=debug|mtp|seq|ref` selects the build.
- `AS_SEND_LAT=3` is always set.
- NVLS (`AS_NVLS_ENABLE=1`) only works within one server: across servers SimCCL generates no
  flows at all.

## TwiNS-RDMA against simai-ref

A SimAI topology file switches twins-rdma to ns-3-alibabacloud's behaviour (`Params::simai`):
- header sizes;
- tx times `floor(bytes * 8e9 / bps)`;
- the host wake-up that counts queue pairs that have sent everything;
- `GLOBAL_T 0`;
- ECN stream ids with `pCnt` 1025.

`--sent=F` writes every flow's first `SendComplete`.

`check.sh JOBFILE` compares the completion and send-completion times of every flow with
`out/simai/ref` (made by `run_ref.sh` on a87). `workloads/simai_topo.py` converts TwiNS
topologies to SimAI's format.

| job file | runs | contents | identical |
|---|---|---|---|
| `jobs_net1.txt` | 13 | FatTree k=4 and k=8 in SimAI's format, uniform links (100 Gb/s at 1 µs, 400 Gb/s at 500 ns); SimAI.conf (MTU 9000, DCQCN, `GLOBAL_T 0`) and variants: MTU 1000, HPCC, `GLOBAL_T 1`, 1–2 MB buffers (up to 2456 pauses) | all |
| `jobs_net2.txt` | 15 | FatTree k=4 and k=8 with host links and switch links of different rates (25–400 Gb/s) and delays (100 ns–2 µs, up to 20× apart); DCQCN and HPCC, 1–2 MB buffers | all |
| `jobs_gpu1.txt` | 17 | SimAI's own topologies (`gen_Topo_Template.py`): Spectrum-X, AlibabaHPN (dual ToR: two NICs per GPU) and DCN+ with 16 GPUs, Spectrum-X with 128; NVSwitches, NVLink at 2400–2880 Gb/s and 25 ns; rings, cross-server pairs, all-to-all within servers, incasts, a mix of 200 flows; MTU 9000 and 1000 | all |

The 28 runs also give identical results as one batch (`check_batch.sh` with `REF=out/simai/ref
CFG=validation/simai/SimAI.conf OUT=out/simai/batch DATA=data/simai:data/rdma:data/topo:data/flows`).

**Links of different delays.** The slot is the shortest link delay (of all worlds of a batch).
- Each link files its frames under the slot they arrive in: a ring of buckets, deep enough for its
  delay plus its longest frame (`LinkInfo`).
- Each switch port has its own headroom, `pfc_a_shift` and ECN thresholds (`PortCfg`), as
  `SetupNetwork` configures them.
- Pair RTTs, BDPs and bandwidths follow `CalculateRoute`'s path sums.

**GPU servers.**
- **NVSwitches** are switches without PFC frames, ECN marking or INT. They keep the MMU's
  admission and drops.
- **A GPU** is a host with a device per link (NVLink, one or two NICs), each with its own
  QbbNetDevice and egress queue. Its queue pairs share RdmaHw.
- **Device choice.** A flow's sender sends on the device `GetNicIdxOfQp` picks, and its receiver
  ACKs on the device `GetNicIdxOfRxQp` picks:
  - the NVSwitch table within a server, the switch table otherwise, from `CalculateRoute`'s next
    hops (an NVSwitch hop replaces the others);
  - among several, by ns-3's `Hash32` (Murmur3) of the queue pair's 4-tuple.
- **Kernels.** GPUs run in a kernel of their own (`rdma_slot_m`), as do switches with more than 64
  ports (`rdma_slot_big`), alongside the main kernel: their larger state would otherwise slow every
  block.

**Ties in lockstep traffic.** Symmetric traffic makes many events simultaneous whose lineages agree
for hundreds of events. NVLink transfers are an example: every frame takes one slot.
- Deciding such ties walked the chains deep into the tail ring: 2.7 M cycles per event selection on
  an all-to-all within servers.
- Each chain now carries a hash of its leads above its root, and its ancestors' hashes four levels
  down.
- Chains of equal leads on distinct roots compare as their roots' start chains.
- Decisions made by a lead are remembered by the pair of hashes (`ccmp`), and lookups strip equal
  leading elements. The comparisons of two ports' lineages slot after slot are then mostly lookups.
- The memo key is 64 bits. A hash collision, about 2^-64 per lookup, would decide a tie wrongly.

Not modelled yet: NVLS (SimAI generates no flows with it across servers anyway).
