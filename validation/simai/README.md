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
  `validation/hpcc/hpcc.patch` does for the HPCC fork.
- **Two crashes fixed.** Both change only runs that crash in stock SimAI.
  - **PFC.** `QbbNetDevice::Resume` asks `m_rdmaEQ->GetQp(qIndex)` whether NVLS is on before it
    checks the node type. A switch port has no `m_rdmaEQ`, so the first RESUME frame a switch
    receives crashes SimAI; a host may have fewer than `qIndex + 1` queue pairs. The patch checks
    for an NVSwitch first.
  - **HPCC.** The switch writes INT at `ppp + 20 + 8 + 6`, the HPCC fork's offset, but SimAI's SeqTs
    header is 10 bytes. INT overwrites the data frame's sequence number and priority, and the next
    switch enqueues the frame into queue 168. The patch writes INT at `+ 10`.
- The ECN draws then no longer depend on how many draws happened before anywhere in the network.
- That is what lets twins-rdma reproduce them.

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

Not modelled yet: links of different rates or delays, NVSwitches, GPUs with two ports, NVLS.
