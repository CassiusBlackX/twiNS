# TwiNS on more topologies

TwiNS's TCP model and twins-rdma take any topology file (`workloads/topo.py`). This page runs them,
and the baselines that can, on the data-center and HPC topologies in common use, at about 128, 1024
and 8192 hosts. `workloads/gen_topo_eval.sh` makes every topology and workload below.

## Result

- **Exact everywhere it was compared.** Against sequential ns-3.46.1: 58 of 58 TCP runs (DCTCP and
  NewReno) identical per flow, on 12 topologies of about 128 and 1024 hosts. Against the HPCC fork:
  32 of 32 RDMA runs (DCQCN, and HPCC where its INT fits) identical per flow, on the same 12.
- **Faster everywhere.** At 512–1,056 hosts, TwiNS (RX 9060 XT) against ns-3 on one core: 301–790×
  on the all-to-all workloads, 5–10× on the 160-flow incasts; against Unison on 32 threads: 14–71×
  and 6–17×. At about 128 hosts: 41–158× and 5–7× against ns-3.
  twins-rdma against the HPCC fork on one core: 28–314×.
- **What each simulator can run.**
  - GeDES builds only FatTrees (`topology.cc`: the other branch is empty), so it is not in these tables.
  - Sequential ns-3 does not reach 8192 hosts: its global routing alone takes about an hour there
    (`hx4d`: 3588 s of set-up before 19 s of simulation, on 32 threads).
  - TwiNS's TCP model handles switches of at most 32 ports (a block's lanes, `src/main.cu`), so
    two-tier leaf-spines stop at 768 hosts and HyperX at 8192 hosts needs four dimensions. twins-rdma
    has no such limit (wide switches: up to 256 ports).
  - HPCC's INT records 5 switches, so tori (paths of 13–17 switches) run DCQCN only.

## The topologies

| name | topology | hosts | switch ports | longest path (switches) |
|---|---|---|---|---|
| ls8o | leaf-spine, 8 leaves of 12 hosts, 4 spines (3:1) | 96 | 16 | 3 |
| t3d4 | 3-D torus 4×4×4, 2 hosts a switch | 128 | 8 | 7 |
| hx4 | HyperX 4×4, 8 hosts a switch | 128 | 14 | 3 |
| xp4 | Xpander, d=4, lift 8 (40 switches), 3 hosts a switch | 120 | 7 | 6 |
| ls32 | leaf-spine, 32 leaves of 16 hosts, 16 spines | 512 | 32 | 3 |
| ls32o | leaf-spine, 32 leaves of 24 hosts, 8 spines (3:1) | 768 | 32 | 3 |
| jf256 | Jellyfish, 256 switches, 12 network ports, 4 hosts each | 1024 | 16 | 5 |
| tor16 | 2-D torus 16×16, 4 hosts a switch | 1024 | 8 | 17 |
| t3d8 | 3-D torus 8×8×8, 2 hosts a switch | 1024 | 8 | 13 |
| df8 | Dragonfly a=8 g=33 h=4 p=4 | 1056 | 15 | 4 |
| hx12 | HyperX 12×12, 7 hosts a switch | 1008 | 29 | 3 |
| xp8 | Xpander, d=8, lift 28, 4 hosts a switch | 1008 | 12 | 5 |
| jf2048 | Jellyfish, 2048 switches, 12 network ports | 8192 | 16 | 6 |
| tor32 | 2-D torus 32×32, 8 hosts a switch | 8192 | 12 | 33 |
| t3d16 | 3-D torus 16×16×16, 2 hosts a switch | 8192 | 8 | 25 |
| df16 | Dragonfly a=16 g=65 h=8 p=8 | 8320 | 27 | 4 |
| hx4d | HyperX 4×4×8×8, 8 hosts a switch | 8192 | 28 | 5 |
| xp16 | Xpander, d=16, lift 120, 4 hosts a switch | 8160 | 20 | 5 |

FatTrees (k=8, 16, 32) are in the README. Links are 100 Gb/s with 1 µs delay, as everywhere in TwiNS's TCP model.

**Workloads**, on every topology:
- `a2a30`: all-to-all, Poisson arrivals at 30% of each host's link for 1 ms, web-search flow sizes;
- `incast16`: 10 rounds, every 200 µs, of 16 senders to one receiver, 64 packets each;
- `hdp30`: all-to-all, Poisson at 30% for 1 ms, Facebook Hadoop flow sizes (`docs/workloads.md`).

## TCP: speed

TwiNS on the RX 9060 XT, the event loop, min of 3 runs. ns-3.46.1 (`validation/ns3/twins-ref.cc`) and
Unison (the same scenario, `--threads=32`) on a87's Xeon Gold 5218, their event loops; the
sequential runs 8 at a time, the Unison ones alone. a87 was shared with another user's VM (16 vCPUs).

| topology | hosts | workload | TCP | flows | ns-3 (s) | Unison 32 thr (s) | TwiNS (s) | vs ns-3 | vs Unison |
|---|---|---|---|---|---|---|---|---|---|
| leaf-spine, 8 leaves x 12 hosts, 4 spines (3:1) | 96 | a2a30 | dctcp | 204 | 19.8 | — | 0.485 | 41× | — |
| leaf-spine, 8 leaves x 12 hosts, 4 spines (3:1) | 96 | a2a30 | newreno | 204 | 36.6 | — | 0.531 | 69× | — |
| leaf-spine, 8 leaves x 12 hosts, 4 spines (3:1) | 96 | incast16 | dctcp | 160 | 0.7 | — | 0.133 | 6× | — |
| leaf-spine, 8 leaves x 12 hosts, 4 spines (3:1) | 96 | incast16 | newreno | 160 | 0.8 | — | 0.159 | 5× | — |
| leaf-spine, 8 leaves x 12 hosts, 4 spines (3:1) | 96 | hdp30 | dctcp | 2968 | 24.3 | — | 0.470 | 52× | — |
| leaf-spine, 8 leaves x 12 hosts, 4 spines (3:1) | 96 | hdp30 | newreno | 2968 | 33.8 | — | 0.524 | 64× | — |
| 3-D torus 4x4x4, 2 hosts a switch | 128 | a2a30 | dctcp | 286 | 38.7 | — | 0.495 | 78× | — |
| 3-D torus 4x4x4, 2 hosts a switch | 128 | a2a30 | newreno | 286 | 83.3 | — | 0.528 | 158× | — |
| 3-D torus 4x4x4, 2 hosts a switch | 128 | incast16 | dctcp | 160 | 0.9 | — | 0.136 | 7× | — |
| 3-D torus 4x4x4, 2 hosts a switch | 128 | incast16 | newreno | 160 | 1.0 | — | 0.177 | 6× | — |
| 3-D torus 4x4x4, 2 hosts a switch | 128 | hdp30 | dctcp | 3910 | 48.0 | — | 0.470 | 102× | — |
| 3-D torus 4x4x4, 2 hosts a switch | 128 | hdp30 | newreno | 3910 | 86.5 | — | 0.554 | 156× | — |
| HyperX 4x4, 8 hosts a switch | 128 | a2a30 | dctcp | 286 | 27.3 | — | 0.463 | 59× | — |
| HyperX 4x4, 8 hosts a switch | 128 | a2a30 | newreno | 286 | 59.9 | — | 0.647 | 93× | — |
| HyperX 4x4, 8 hosts a switch | 128 | incast16 | dctcp | 160 | 0.7 | — | 0.124 | 6× | — |
| HyperX 4x4, 8 hosts a switch | 128 | incast16 | newreno | 160 | 0.7 | — | 0.148 | 5× | — |
| HyperX 4x4, 8 hosts a switch | 128 | hdp30 | dctcp | 3910 | 35.1 | — | 0.541 | 65× | — |
| HyperX 4x4, 8 hosts a switch | 128 | hdp30 | newreno | 3910 | 53.6 | — | 0.606 | 88× | — |
| Xpander, d=4, lift 8, 3 hosts a switch | 120 | a2a30 | dctcp | 259 | 26.4 | — | 0.588 | 45× | — |
| Xpander, d=4, lift 8, 3 hosts a switch | 120 | a2a30 | newreno | 259 | 47.1 | — | 0.677 | 70× | — |
| Xpander, d=4, lift 8, 3 hosts a switch | 120 | incast16 | dctcp | 160 | 0.8 | — | 0.131 | 6× | — |
| Xpander, d=4, lift 8, 3 hosts a switch | 120 | incast16 | newreno | 160 | 0.8 | — | 0.162 | 5× | — |
| Xpander, d=4, lift 8, 3 hosts a switch | 120 | hdp30 | dctcp | 3655 | 40.1 | — | 0.413 | 97× | — |
| Xpander, d=4, lift 8, 3 hosts a switch | 120 | hdp30 | newreno | 3655 | 52.2 | — | 0.545 | 96× | — |
| leaf-spine, 32 leaves x 16 hosts, 16 spines | 512 | a2a30 | dctcp | 1182 | 214.1 | 9.7 | 0.712 | 301× | 14× |
| leaf-spine, 32 leaves x 16 hosts, 16 spines | 512 | a2a30 | newreno | 1182 | 363.8 | 36.8 | 0.766 | 475× | 48× |
| leaf-spine, 32 leaves x 16 hosts, 16 spines | 512 | incast16 | dctcp | 160 | 1.1 | 1.0 | 0.124 | 9× | 8× |
| leaf-spine, 32 leaves x 16 hosts, 16 spines | 512 | incast16 | newreno | 160 | 1.0 | 1.0 | 0.163 | 6× | 6× |
| leaf-spine, 32 leaves x 16 hosts, 16 spines | 512 | hdp30 | dctcp | 15483 | 228.0 | 9.0 | 0.572 | 399× | 16× |
| leaf-spine, 32 leaves x 16 hosts, 16 spines | 512 | hdp30 | newreno | 15483 | 339.9 | 23.4 | 0.687 | 495× | 34× |
| leaf-spine, 32 x 24 hosts, 8 spines (3:1) | 768 | a2a30 | dctcp | 1753 | 285.6 | 14.0 | 0.836 | 342× | 17× |
| leaf-spine, 32 x 24 hosts, 8 spines (3:1) | 768 | a2a30 | newreno | 1753 | 436.3 | 56.4 | 1.305 | 334× | 43× |
| leaf-spine, 32 x 24 hosts, 8 spines (3:1) | 768 | incast16 | dctcp | 160 | 1.0 | 1.3 | 0.131 | 7× | 10× |
| leaf-spine, 32 x 24 hosts, 8 spines (3:1) | 768 | incast16 | newreno | 160 | 1.0 | 1.3 | 0.151 | 7× | 9× |
| leaf-spine, 32 x 24 hosts, 8 spines (3:1) | 768 | hdp30 | dctcp | 23222 | 361.3 | 12.7 | 0.665 | 544× | 19× |
| leaf-spine, 32 x 24 hosts, 8 spines (3:1) | 768 | hdp30 | newreno | 23222 | 403.9 | 27.9 | 0.914 | 442× | 31× |
| Jellyfish, 256 switches, 12 network ports | 1024 | a2a30 | dctcp | 2316 | 376.3 | 19.1 | 0.859 | 438× | 22× |
| Jellyfish, 256 switches, 12 network ports | 1024 | a2a30 | newreno | 2316 | 682.2 | 75.1 | 1.190 | 573× | 63× |
| Jellyfish, 256 switches, 12 network ports | 1024 | incast16 | dctcp | 160 | 1.0 | 2.0 | 0.141 | 7× | 14× |
| Jellyfish, 256 switches, 12 network ports | 1024 | incast16 | newreno | 160 | 1.0 | 2.3 | 0.172 | 6× | 14× |
| Jellyfish, 256 switches, 12 network ports | 1024 | hdp30 | dctcp | 30711 | 412.5 | 18.2 | 0.652 | 633× | 28× |
| Jellyfish, 256 switches, 12 network ports | 1024 | hdp30 | newreno | 30711 | 616.3 | 53.6 | 0.950 | 649× | 56× |
| 2-D torus 16x16, 4 hosts a switch | 1024 | a2a30 | dctcp | 2317 | 739.9 | 35.8 | 1.277 | 579× | 28× |
| 2-D torus 16x16, 4 hosts a switch | 1024 | a2a30 | newreno | 2317 | 754.9 | 57.1 | 1.702 | 444× | 34× |
| 2-D torus 16x16, 4 hosts a switch | 1024 | incast16 | dctcp | 160 | 1.4 | 2.2 | 0.133 | 10× | 16× |
| 2-D torus 16x16, 4 hosts a switch | 1024 | incast16 | newreno | 160 | 1.4 | 2.4 | 0.171 | 8× | 14× |
| 2-D torus 16x16, 4 hosts a switch | 1024 | hdp30 | dctcp | 30711 | 728.0 | 35.2 | 1.001 | 727× | 35× |
| 2-D torus 16x16, 4 hosts a switch | 1024 | hdp30 | newreno | 30711 | 746.1 | 57.9 | 1.674 | 446× | 35× |
| 3-D torus 8x8x8, 2 hosts a switch | 1024 | a2a30 | dctcp | 2317 | 544.3 | 30.1 | 0.968 | 562× | 31× |
| 3-D torus 8x8x8, 2 hosts a switch | 1024 | a2a30 | newreno | 2317 | 795.6 | 72.1 | 1.407 | 566× | 51× |
| 3-D torus 8x8x8, 2 hosts a switch | 1024 | incast16 | dctcp | 160 | 1.2 | 2.5 | 0.147 | 8× | 17× |
| 3-D torus 8x8x8, 2 hosts a switch | 1024 | incast16 | newreno | 160 | 1.2 | 3.0 | 0.173 | 7× | 17× |
| 3-D torus 8x8x8, 2 hosts a switch | 1024 | hdp30 | dctcp | 30711 | 597.4 | 28.1 | 0.756 | 790× | 37× |
| 3-D torus 8x8x8, 2 hosts a switch | 1024 | hdp30 | newreno | 30711 | 821.7 | 52.0 | 1.164 | 706× | 45× |
| Dragonfly a=8 g=33 h=4 p=4 | 1056 | a2a30 | dctcp | 2280 | 335.6 | 18.5 | 0.888 | 378× | 21× |
| Dragonfly a=8 g=33 h=4 p=4 | 1056 | a2a30 | newreno | 2280 | 560.0 | 71.0 | 1.261 | 444× | 56× |
| Dragonfly a=8 g=33 h=4 p=4 | 1056 | incast16 | dctcp | 160 | 0.7 | 2.1 | 0.147 | 5× | 14× |
| Dragonfly a=8 g=33 h=4 p=4 | 1056 | incast16 | newreno | 160 | 0.8 | 2.5 | 0.171 | 5× | 14× |
| Dragonfly a=8 g=33 h=4 p=4 | 1056 | hdp30 | dctcp | 32075 | 446.9 | 20.0 | 0.663 | 674× | 30× |
| Dragonfly a=8 g=33 h=4 p=4 | 1056 | hdp30 | newreno | 32075 | 625.6 | 50.0 | 0.898 | 697× | 56× |
| HyperX 12x12, 7 hosts a switch | 1008 | a2a30 | dctcp | 2335 | 314.8 | 17.6 | 0.848 | 371× | 21× |
| HyperX 12x12, 7 hosts a switch | 1008 | a2a30 | newreno | 2335 | 593.7 | 83.5 | 1.172 | 507× | 71× |
| HyperX 12x12, 7 hosts a switch | 1008 | incast16 | dctcp | 160 | 0.7 | 1.6 | 0.141 | 5× | 11× |
| HyperX 12x12, 7 hosts a switch | 1008 | incast16 | newreno | 160 | 0.8 | 1.7 | 0.155 | 5× | 11× |
| HyperX 12x12, 7 hosts a switch | 1008 | hdp30 | dctcp | 30551 | 340.4 | 16.6 | 0.650 | 524× | 26× |
| HyperX 12x12, 7 hosts a switch | 1008 | hdp30 | newreno | 30551 | 537.2 | 46.8 | 0.844 | 636× | 55× |
| Xpander, d=8, lift 28, 4 hosts a switch | 1008 | a2a30 | dctcp | 2335 | 315.7 | 21.2 | 0.824 | 383× | 26× |
| Xpander, d=8, lift 28, 4 hosts a switch | 1008 | a2a30 | newreno | 2335 | — | 67.4 | 1.158 | — | 58× |
| Xpander, d=8, lift 28, 4 hosts a switch | 1008 | incast16 | dctcp | 160 | 0.7 | 1.9 | 0.136 | 5× | 14× |
| Xpander, d=8, lift 28, 4 hosts a switch | 1008 | incast16 | newreno | 160 | 0.8 | 2.2 | 0.154 | 5× | 14× |
| Xpander, d=8, lift 28, 4 hosts a switch | 1008 | hdp30 | dctcp | 30551 | 345.8 | 19.2 | 0.632 | 547× | 30× |
| Xpander, d=8, lift 28, 4 hosts a switch | 1008 | hdp30 | newreno | 30551 | — | 48.2 | 0.916 | — | 53× |
| HyperX 4x4x8x8, 8 hosts a switch | 8192 | incast16 | dctcp | 160 | — | 19.2 | 0.165 | — | 117× |

## RDMA: exactness and speed

twins-rdma on the RX 9060 XT (process wall time, min of 2), against the HPCC fork on one core of
a87 (8 at a time). `validation/hpcc/jobs_topo.txt`.

| topology | workload | CC | flows | identical | HPCC fork (s) | twins-rdma (s) | speedup |
|---|---|---|---|---|---|---|---|
| ls8o | a2a30 | DCQCN | 204 | all | 16.7 | 0.59 | 28× |
| t3d4 | a2a30 | DCQCN | 286 | all | 26.8 | 0.59 | 45× |
| hx4 | a2a30 | DCQCN | 286 | all | 23.2 | 0.52 | 45× |
| xp4 | a2a30 | DCQCN | 259 | all | 19.2 | 0.61 | 32× |
| ls32 | a2a30 | DCQCN | 1182 | all | 98.6 | 0.93 | 106× |
| ls32 | a2a30 | HPCC | 1182 | all | 97.9 | 1.14 | 86× |
| ls32 | hdp30 | DCQCN | 15483 | all | 104.3 | 0.79 | 132× |
| ls32 | hdp30 | HPCC | 15483 | all | 101.2 | 1.02 | 99× |
| ls32o | a2a30 | DCQCN | 1753 | all | 133.3 | 1.42 | 94× |
| ls32o | a2a30 | HPCC | 1753 | all | 141.5 | 1.87 | 76× |
| ls32o | hdp30 | DCQCN | 23222 | all | 156.8 | 1.91 | 82× |
| ls32o | hdp30 | HPCC | 23222 | all | 132.8 | 1.51 | 88× |
| jf256 | a2a30 | DCQCN | 2316 | all | 248.7 | 1.16 | 214× |
| jf256 | a2a30 | HPCC | 2316 | all | 247.5 | 1.41 | 176× |
| jf256 | hdp30 | DCQCN | 30711 | all | 231.6 | 1.02 | 227× |
| jf256 | hdp30 | HPCC | 30711 | all | 239.1 | 1.20 | 199× |
| tor16 | a2a30 | DCQCN | 2317 | all | 587.4 | 2.10 | 280× |
| tor16 | hdp30 | DCQCN | 30711 | all | 533.2 | 2.17 | 246× |
| t3d8 | a2a30 | DCQCN | 2317 | all | 436.6 | 1.57 | 278× |
| t3d8 | hdp30 | DCQCN | 30711 | all | 426.4 | 1.36 | 314× |
| df8 | a2a30 | DCQCN | 2280 | all | 202.6 | 1.17 | 173× |
| df8 | a2a30 | HPCC | 2280 | all | 232.1 | 1.56 | 149× |
| df8 | hdp30 | DCQCN | 32075 | all | 246.0 | 1.07 | 230× |
| df8 | hdp30 | HPCC | 32075 | all | 255.9 | 1.33 | 192× |
| hx12 | a2a30 | DCQCN | 2335 | all | 177.8 | 1.32 | 135× |
| hx12 | a2a30 | HPCC | 2335 | all | 184.9 | 1.69 | 109× |
| hx12 | hdp30 | DCQCN | 30551 | all | 188.7 | 1.13 | 167× |
| hx12 | hdp30 | HPCC | 30551 | all | 187.7 | 1.38 | 136× |
| xp8 | a2a30 | DCQCN | 2335 | all | 224.7 | 1.11 | 202× |
| xp8 | a2a30 | HPCC | 2335 | all | 243.2 | 1.49 | 163× |
| xp8 | hdp30 | DCQCN | 30551 | all | 225.4 | 0.99 | 228× |
| xp8 | hdp30 | HPCC | 30551 | all | 238.9 | 1.22 | 196× |

The 2-D torus with Hadoop traffic first overflowed TwiNS's port queues (4096 frames a port, reported
and marked as not exact): the 32 MB buffers hold more small frames than that on a torus's congested
paths. With `--q3cap=32768` it is identical.
