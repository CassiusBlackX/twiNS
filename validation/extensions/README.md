# TCP and IRN extension validation

## TCP graph links

A graph line such as `link h0 s0 25000000000 500000` gives both directions a rate in bits per second and propagation delay in picoseconds. The two-field form inherits 100 Gb/s and 1 μs. All hosts are single-homed. Run the usual `twins --topo=graph --topofile=... --flows=... --out=...` command. Keep the helper binaries and Python 3 driver in the build directory.

The general kernel uses the minimum propagation delay as its slot. A lookup per distinct rate and wire size reproduces the reference fixed-point serialization time. A 64-bit lead encodes the exact delay plus one, leaving zero for an empty history entry. A receiver slot can contain frames sent several slots ago. The GPU tags the arrival buckets by absolute slot and waits for pending arrivals before skipping idle slots. Ring size grows with the delay spread, slot width, fastest link, number of links and frame layout. The loader checks representable time and bucket geometry. The compact homogeneous fast path remains available.

The default TCP entry point starts with the compact history profile. On an active-flow overflow it allocates host lists from that host's connection inventory. On a tie it adds per-host timer creation order and extends the history through the compact 128-, 256- and 512-byte profiles. Mixed links use successively 3, 7, 15 and 31 exact lead runs. Pass logs print actual frame sizes. A batch repeats only affected worlds. Capacity retries restart from the original inputs. The driver reports cumulative simulation time and process wall time, including failed passes. An incomplete final pass or unresolved history returns a nonzero exit status. Use `--single-pass` for raw diagnostics.

To reproduce the queue comparison, build ns-3.46.1 using both routing patches in [the reference instructions](../README.md). From this checkout run the following with fresh output directories.

```sh
python validation/extensions/tcp_links.py build/twins /path/to/ns3.46.1-twins-ref-optimized out/tcp-links
```

The matrix compares all six flow fields and the time, device, order, packet identity and queue length of each enqueue through the last completion expressed in nanoseconds. It covers uniform, rate-only, delay-only and mixed links. A fifth input uses 10 hosts per leaf, 625 Gb/s access, 500 Gb/s uplinks, 12-packet queues and a simultaneous burst. Every variant runs CUBIC, DCTCP and NewReno with flow ECMP and packet spraying. Repeat with `--scale` and a new directory for the 128-host, eight-leaf, four-spine case.

## Loss-tolerant RDMA

Enable `ENABLE_IRN=1 ENABLE_PFC=0 CC_MODE=1` in the usual RDMA or AI invocation. The NIC uses separate sender and receiver SACK bitmaps, cumulative feedback, selective retransmission, BDP flow control and a per-QP low/high timeout. The switch uses the Broadcom admission rules of the reference fork. The explicit last-hop notification of QP completion reproduces receiver deletion and recreation on a delayed duplicate. IRN uses single-slot execution for this dependency. Bitmap storage is proportional to the total number of packet segments across the configured flows. IRN accepts 32-bit byte sequence numbers and uses GPU network execution for AI. It does not implement ConWeave load balancing.

The mechanism is IRN (SIGCOMM 2018). Its executable reference is the IRN implementation in the authors' ConWeave artifact at revision `236a801a00e35de9078635e04acae2f701c21ded`. The trace patch only attaches a transmission observer. [IRN paper](https://doi.org/10.1145/3230543.3230557), [pinned reference](https://github.com/conweave-project/conweave-ns3/tree/236a801a00e35de9078635e04acae2f701c21ded).

```sh
git -C /path/to/conweave-ns3 apply "$PWD/validation/extensions/irn-trace.patch"
# Build the fork using its Python 2 waf and GCC toolchain.
(cd /path/to/conweave-ns3 && python2 waf configure -d optimized && python2 waf build)
python validation/extensions/irn_fixture.py /path/to/conweave-ns3 out/irn
python validation/extensions/irn_compare.py build/twins-rdma /path/to/conweave-ns3 out/irn
```

The comparison also accepts `--image IMAGE` for a local runtime container and repeatable `--case NAME` filters. It records both process times, binary hash, commands, per-flow identity and FCT agreement, and the first difference in link transmissions. Reference process time in container mode includes container start and stop. For IRN, both traces also expose the destination port, selective-feedback byte offset and length, and CNP bit. The six fixtures cover a short final packet, multiple destination ports, cross-rack routing, probabilistic ECN, lossy incasts with timeout and SACK, and eight simultaneous 15-to-1 incasts. The AI AllReduce control can be checked against existing loss-free SimAI receipts. A lossy closed-loop AI run exercises the shared transport independently of those receipts.

For native timing, generate a fresh fixture directory and run `irn_timing.py reference REF FIXTURES` in the reference runtime and `irn_timing.py twins TWINS FIXTURES` on the GPU host. Trial zero warms each scenario. The next three trials time complete processes without packet tracing. Each saves its FCT file and log.

## Frozen audit

Run the following from the checkout containing the archived evidence and its referenced data. The output paths must be fresh. The replay keeps every process log and flow file, the executable hashes and elapsed wall clock. The report checks inputs, reference hashes, terminal counters, output multiplicity and coverage of the frozen configuration IDs.

```sh
python /path/to/extensions/audit.py /path/to/tcp-build /data/a4-tcp --kind tcp
python /path/to/extensions/audit.py /path/to/rdma-build /data/a4-rdma --kind rdma
python /path/to/extensions/report.py out/paper-evidence-20261116/reports/unified-manifest.json /data/a4-tcp /data/a4-rdma /data/a4-verdict.json
```

For whole-process TCP timings, run `tcp_timing.py TWINS NS3 SCALE_TOPOLOGY OUT` with the graph generated by `tcp_links.py --scale`. The 128 flows each send 1 MiB plus a flow-specific tail. The harness alternates engine order, compares every flow on each trial and keeps the warm-up separate.
