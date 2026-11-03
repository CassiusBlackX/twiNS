# TwiNS on NVIDIA (RTX 5090)

**Result.** On an RTX 5090, TwiNS gives the same results as on the RX 9060 XT: every validation
set is identical to its reference. Exactness therefore holds across GPU vendors and compilers
(HIP/ROCm for gfx1200, CUDA 13.0 for sm_120).

Sequential ns-3 on a second CPU, a Ryzen 9 9950X (Zen 5), also reproduces the stored references
bit for bit. The exactness target is ns-3's output, not one machine's.

## Machine and build

- GPU: RTX 5090, 32 GB, driver 595.99, CUDA 13.0.
- CPU: Ryzen 9 9950X (16 cores, 5.7 GHz boost), 91 GB, Ubuntu 24.04. It has the same GCC 13.3
  and glibc 2.39 as a87.

```shell
cmake -S . -B build -DTWINS_GPU_BACKEND=CUDA -DCMAKE_CUDA_ARCHITECTURES=120
cmake --build build -j
```

The port touched only `src/gpu_runtime.h` and one intrinsic:
- `gpu_runtime.h` maps twins-rdma's `hip*` names to CUDA;
- the persistent kernel's wait uses `__nanosleep` instead of `s_sleep`.

A block is 32 lanes, one wave on RDNA and one warp on NVIDIA. Lanes share data only through
`__syncthreads`, which is why independent thread scheduling needed no change.

## Exactness

| set | runs | identical |
|---|---|---|
| TCP: `validation/check_all.sh` (k=8 with jitter variants, k=16, leaf-spine, non-MSS sizes, reuse/FIN, spraying, topology files, failures, general kernel) | 306 | 306 |
| TCP sweeps: k=8 (138 configurations) and k=16 (48) | 186 | 186 |
| RDMA (HPCC fork): `jobs1`, `jobs2`, `jobs3`, and the k=8 sweep as one batch (32 worlds) | 47 + 32 | all |
| SimAI: `jobs_net1`, `net2`, `gpu1`, `dag1`, `scale` | 60 | all |
| SimAI end to end: `jobs_e2e` (microAllReduce up to 128 GPUs, AICB Megatron/MoE) | 16 | 16 |
| SimAI, new scale: Spectrum-X, 2048 and 4096 GPUs, rail rings (1,044,480 and 4,186,112 messages) | 2 | 2 |
| RDMA, new scale: FatTree k=32 (8,192 hosts), HPCC-fork references (342,968 flows) | 5 | 5 |

Notes:
- The same undecided ties appear as on AMD: 3 worlds of the k=8 sweep and 8 of the k=16 sweep.
  twins128 decides them as before.
- ns-3 on the 9950X reproduces every stored reference it was run on: 18 k=8 runs (6 workloads,
  3 TCPs) and 8 k=16 runs. The stored references were run on a87, a Xeon Gold 5218.

## Speed

### TCP

Wall time of the event loop, min of 3 runs. The CSV is `data/twins_speed_5090.csv`.

The sequential ns-3 baseline is the 9950X, one run at a time. That is about 2.5× faster per run
than the Xeon in the README. Unison stays measured on a87 (32 threads); see the README.

| workload | TCP | RX 9060 XT | RTX 5090 | 5090 vs AMD | ns-3 on 9950X | vs ns-3 (9950X) |
|---|---|---|---|---|---|---|
| k8 paper | DCTCP | 0.224 s | 0.141 s | 1.6× | 9.78 s | 69× |
| k8 paper | NewReno | 0.365 s | 0.224 s | 1.6× | 15.3 s | 68× |
| k8 fullsync | DCTCP | 0.035 s | 0.019 s | 1.8× | 1.12 s | 60× |
| k8 fullexp | DCTCP | 0.075 s | 0.037 s | 2.1× | 1.63 s | 45× |
| k8 a2a30 | DCTCP | 0.326 s | 0.185 s | 1.8× | 8.52 s | 46× |
| k8 a2a30 | NewReno | 0.396 s | 0.237 s | 1.7× | 11.6 s | 49× |
| k8 incast16 | DCTCP | 0.085 s | 0.038 s | 2.2× | 0.28 s | 7× |
| k8 a2a50s | DCTCP | 0.357 s | 0.254 s | 1.4× | 21.8 s | 86× |
| k16 paper | DCTCP | 0.434 s | 0.329 s | 1.3× | 125.6 s | 382× |
| k16 paper | NewReno | 0.551 s | 0.431 s | 1.3× | 178.3 s | 414× |
| k16 a2a30 | DCTCP | 0.637 s | 0.608 s | 1.05× | 123.6 s | 203× |
| k16 a2a30 | NewReno | 0.833 s | 0.748 s | 1.1× | 177.4 s | 237× |
| k16 incast16 | DCTCP | 0.076 s | 0.042 s | 1.8× | 0.36 s | 9× |
| k16 a2a50s | DCTCP | 0.507 s | 0.467 s | 1.1× | 149.7 s | 320× |
| k16 a2a50s | NewReno | 1.050 s | 0.832 s | 1.3× | 145.1 s | 174× |
| k32 paper | DCTCP | 1.196 s | 0.861 s | 1.4× | | |
| k32 paper | NewReno | 1.655 s | 1.198 s | 1.4× | | |
| k32 a2a30 | DCTCP | 0.870 s | 0.843 s | 1.03× | | |
| k32 incast16 | DCTCP | 0.073 s | 0.044 s | 1.7× | | |
| k32 a2a50s | DCTCP | 1.207 s | 0.843 s | 1.4× | | |
| k32 a2a50s | NewReno | 1.502 s | 0.973 s | 1.5× | | |

The AMD column is `data/twins_speed.csv`.

The 5090 gains most on short and small runs: launch overhead and per-slot latency, 1.6–2.2×. On
large busy runs it gains 1.05–1.4×. There the slowest block of each slot sets the pace, not the
GPU's width: the 5090 has 170 SMs to the 9060 XT's 32 CUs, which matters little at that point.

**GeDES on the same GPU** (`specsim-baseline` branch, CUDA, paper workloads, min of 3):
- k=8: 0.318 s;
- k=16: 0.450 s;
- k=32: 156.5 s.

TwiNS on the same workloads takes 0.141, 0.329 and 0.861 s. So on NVIDIA:
- GeDES is close to TwiNS at k ≤ 16 (2.3× and 1.4× slower);
- it collapses at k=32 (182× slower);
- the stock GeDES CUDA tree shows the same k=32 behaviour (165 s).

On AMD, GeDES took 1.65, 2.51 and 15.9 s. GeDES's model remains 15–92% off ns-3 in mean FCT
either way.

**Beyond k=32.** A FatTree stops at k=32 because a switch's ports fill one 32-lane block. A
topology file can still hold many more hosts with switches of at most 32 ports.

The runs below use Jellyfish graphs, random regular, with 16 network ports per switch (generator:
random stub matching, as `topo.py jellyfish` is quadratic). The traffic is websearch all-to-all at
30% load over 0.5 ms (`gen.py --pattern alltoall --load 0.3 --duration_ns 500000`), as in the
a2a30 workloads.

These sizes have no reference: ns-3's set-up alone is hours at 8,192 hosts. So they show scale
and speed only. GPU memory is the peak during the run.

| switches × hosts each | hosts | flows | DCTCP | NewReno | GPU memory |
|---|---|---|---|---|---|
| 512 × 16 | 8,192 | 8,978 | 0.61 s | 0.95 s | 3.3 GB |
| 1024 × 16 | 16,384 | 17,793 | 0.71 s | 1.13 s | 5.8 GB |
| 2048 × 16 | 32,768 | 35,783 | 0.97 s | 1.37 s | 10.8 GB |
| 4096 × 15 | 61,440 | 67,473 | 1.37 s | 2.01 s | 19.9 GB |

Notes:
- 61,440 hosts is close to the 16-bit host-id limit.
- The NewReno run at 32,768 hosts reports 3 undecided ties, so twins128 should be used there.

### Parameter sweeps (batches of worlds)

All outputs are identical to ns-3. The times include process start and the twins128 reruns.

| sweep | RX 9060 XT (16 GB) | RTX 5090, same batches | RTX 5090, larger batches |
|---|---|---|---|
| k=8, 138 configurations | 6.2 s (2 × 69 worlds) | 3.42 s | 2.64 s (1 × 138 worlds) |
| k=16, 48 configurations | 16.0 s (3 × 16 worlds) | 7.79 s | 6.59 s (2 × 24 worlds) |

Against ns-3 on 32 Xeon cores (455.7 s and 1,953.5 s), that is 173× and 296×. A batch of 48 k=16
worlds does not fit in 32 GB.

### RDMA (twins-rdma vs the HPCC fork)

The times are process wall times, min of 3. They include set-up and file output, as in the
README.

| run | RX 9060 XT | RTX 5090 |
|---|---|---|
| k16 a2a30, DCQCN | 0.99 s | 1.03 s |
| k16 a2a30, HPCC | 1.14 s | 1.14 s |
| k16 a2a50s, DCQCN, 2 MB buffers | 1.17 s | 1.67 s |
| k8 a2a50s, HPCC | 0.44 s | 0.59 s |
| k8 permutation, HPCC | 0.094 s | 0.26 s |
| leaf-spine a2a30, DCQCN | 0.63 s | 0.81 s |
| Jellyfish a2a30, HPCC | 0.76 s | 0.83 s |
| k8 incast64, 1 MB buffers, DCQCN | 0.92 s | 0.90 s |
| k8 sweep, 32 worlds in one batch | 3.2 s | 1.62 s |

**New scale: k=32 (8,192 hosts).** These are new HPCC-fork references, run sequentially in their
container on the 9950X with memory caps.

| run | flows | identical | reference | twins-rdma (process / simulation) | speedup (process / simulation) |
|---|---|---|---|---|---|
| k32 a2a30, DCQCN (68 marks) | 8,899 | 8,899 | 810.4 s | 3.26 s / 1.10 s | 249× / 737× |
| k32 a2a30, HPCC | 8,899 | 8,899 | 772.1 s | 3.47 s / 1.29 s | 222× / 598× |
| k32 incast16, DCQCN (853 marks) | 160 | 160 | 400.0 s | 2.29 s / 0.118 s | 175× / 3400× |
| k32 a2a50s, HPCC (4404 marks) | 162,505 | 162,505 | 764.6 s (peak 50.5 GB) | 3.99 s / 1.63 s | 191× / 469× |
| k32 a2a50s, DCQCN (2838 marks) | 162,505 | 162,505 | 716.1 s (peak 50.2 GB) | 4.15 s / 1.76 s | 173× / 407× |

Notes:
- Each reference peaked at about 49 GB of RAM.
- The twins-rdma times are the min of 3, with commit 596f432. That commit took k=32 set-up from
  about 8.6 s to about 2.2 s, by:
  - running the routes' BFS over flat adjacency lists;
  - making the per-pair tables flat, with one row per BFS;
  - keeping per-pair next hops only for hosts with several links;
  - writing switch next hops straight as ports;
  - computing the MRG32k3a jump matrices once instead of per port.
- Every validation set is identical with it.

Single RDMA runs are no faster on the 5090.
- In small runs the process overhead dominates. The k8 permutation's simulation loop takes
  0.062 s, and the rest of its 0.26 s is set-up (CUDA context, graph capture).
- Some large single runs are slower in the loop itself: k16 a2a50s with PFC takes 1.30 s. Their
  pace is set by the slowest wave per slot (a switch's serial events), and an SM runs a single
  wave no faster than an RDNA CU does.
- Batches, which fill the GPU, run twice as fast.

### SimAI

| run | messages | SimAI reference (one Xeon core) | RX 9060 XT | RTX 5090 |
|---|---|---|---|---|
| Spectrum-X 128, rails | 3,840 | 4.2 s | 1.3 s | 0.70 s |
| Spectrum-X 512, rails | 64,512 | 16.3 s | 2.8 s | 2.00 s |
| Spectrum-X 1024, rails | 260,096 | 39.4 s | 10.0 s | 10.24 s |
| Spectrum-X 2048, rails | 1,044,480 | 150 s | | 15.96 s |
| Spectrum-X 4096, rails | 4,186,112 | 184.8 s (one 9950X core, about 33 GB) | does not fit | 38.9 s simulation (42.9 s process), 14 GB of GPU memory; all identical |

The 2048-GPU reference ran on a loaded a87, so its 150 s is an upper bound. The 4096-GPU
reference ran on a 9950X core, which is much faster per core. Against it TwiNS gains only 4.3×:
these ring runs are latency-bound on the GPU (80,064 slot launches).

End to end (`jobs_e2e`, all identical):
- microAllReduce at 128 GPUs: 4.6–5.9 s, against 5.7–8.0 s on the 9060 XT and 53–63 s for
  SimAI;
- AICB 16-GPU runs: 25–68 s, against 32–122 s on the 9060 XT and 37–122 s for SimAI.

These dense 16-GPU runs remain latency-bound: about 20 entities per 25 ns slot.

## Sequential ns-3 at k=32 does not fit

Sequential ns-3 cannot produce a k=32 (8,192-host) reference on the machines here. Its set-up is
global routing, and its memory grows with nodes × hosts:

| k | hosts | nodes | peak RSS (9950X) |
|---|---|---|---|
| 8 | 128 | 208 | 0.17 GB |
| 16 | 1,024 | 1,344 | 2.55 GB |
| 32 | 8,192 | 9,472 | about 137 GB, extrapolated |

A k=32 paper run on a87 (125 GB) under a 105 GB cap was still in set-up after 2.5 h. It held
98.4 GB and was growing 0.3 GB/min, so I stopped it before it reached the cap. On the 9950X
(91 GB) it hit an 80 GB cap after 30 min.

At k=32, TCP exactness therefore rests on k≤16 plus the k=32 RDMA runs above. The RDMA runs were
checked against the HPCC fork and are identical.
