# Phase 0: SimAI recon notes

SimAI was checked out at `a57d765`, cloned to `a87:~/netsim/simai`. Its submodules:

| submodule | commit |
|---|---|
| ns-3-alibabacloud | 3e0c7c1 |
| SimCCL | fd7cd57 |
| aicb | 23eec3c |
| SimAI-CLEM | 4a91f8b |

## Base of ns-3-alibabacloud

- **ns-3 version.** It is ns-3.36.1, not ns-3.17: the binary is
  `build/scratch/ns3.36.1-AstraSimNetwork-debug`. The HPCC RDMA model is ported to that version:
  qbb-net-device, rdma-hw, rdma-queue-pair, switch-node, switch-mmu, cn/pause/qbb headers, pint.
  New in the port: nvswitch-node, qbb-remote-channel, point-to-point-remote-channel (MPI), and
  trace-format.
- **Simulator kernel.** It is built with `--enable-mtp` (Unison). `AstraSimNetwork.cc` calls
  `MtpInterface::Enable(threads)` for every run, `-t 1` included. So stock SimAI always runs on
  `MultithreadedSimulatorImpl`, never on the sequential `DefaultSimulatorImpl`.
- **Build profile.** The default build is `-d debug`. Speed baselines need an optimized build as
  well.

## Unison (MTP) event order, from `src/mtp/model`

**Partition.** Links with delay at or above the median link delay are cut, and BFS over the
uncut links groups nodes into logical processes (LPs).
- System ids follow node-id order, from 1; LP 0 is the public LP.
- With uniform delays every node is an LP of its own.
- With short NVLink delays a server becomes one LP: its GPUs, NVSwitch and NICs.

**Lookahead.** An LP's lookahead is the smallest delay of its cut links. LP 0 has lookahead 0.

**A round** (`MtpInterface::ProcessOneRound`):
1. Every LP runs its events with `ts <= min(smallest + lookahead, next public time)`.
2. The public LP runs its round.
3. Every LP receives its messages.
4. `smallest` becomes the minimum next event time over all LPs.

**Event keys** are (ts, uid), with a uid counter per LP.
- A local schedule takes `uid = m_uid++`.
- A cross-LP `ScheduleWithContext` goes to the remote LP's mailbox, keyed by the sending LP's
  id, as the tuple (sender ts, sender id, sender `m_uid` without incrementing it).
- `ReceiveMessages` walks the mailboxes in sender-id order (an `std::map`), sorts each by that
  tuple ascending, and gives each event `m_uid++` of the receiver.

**Consequence for TwiNS.** To be exact against stock SimAI, TwiNS must reproduce Unison's order:
- uid counters per entity, which is exact and simple, with no causal chains;
- tags on frames: (sender LP, send time, sender uid);
- adaptive rounds: entity e runs to `smallest + lookahead_e`, and arrivals take uids at the round's
  receive stage.

This is a second ordering mode next to the sequential-ns-3 chains.

**Open question: determinism.** LPs run each round in an order sorted by measured execution time
(`PartitionSchedulingMethod = ByExecutionTime`). That order is irrelevant for state local to an
LP. astra-sim's state is global, though: maps such as `expeRecvHash` are shared across nodes and
changed in callbacks under `explicitCriticalSection`. If callbacks from different LPs in one round
interact, stock SimAI may not be deterministic. Test: run the same input twice and compare.

## SimAI's glue to the network (`network_frontend/ns3/entry.h`)

- **Sending a message.** `SendFlow` installs an `RdmaClient` on the source node (one QP per
  message; `portNumber[src][dst]++`) and calls `appCon.Start(Time(send_lat))`.
  - `send_lat` comes from NCCL latency tables per algorithm and protocol, with separate NVLink
    and network tables, in ns; `AS_SEND_LAT` overrides it, in µs.
  - The code multiplies the value by 1000 ("ns -> ps"), so the time resolution must be checked.
- **Completion callbacks.**
  - `notify_receiver_receive_data` matches `expeRecvHash`/`recvHash` and calls astra-sim's
    `msg_handler` synchronously.
  - `notify_sender_sending_finished` does the same on the sender side.
- **Setup.** Workloads are fired in `main` (`systems[j]->workload->fire()`) before `Run`.
  `Partition()` runs time-0 events directly (`InvokeNow`), in insertion order.

## SimAI.conf defaults that differ from TwiNS's HPCC validation

- `PACKET_PAYLOAD_SIZE 9000`;
- `CC_MODE 1` (DCQCN);
- `GLOBAL_T 0` (per-pair window and base RTT: needs `pairBdp` and `pairRtt`);
- `KMAX/KMIN/PMAX_MAP` for 25G to 1.6T;
- monitors (qlen, bw, rate, cnp) scheduled periodically. They take uids but only read state.

## TwiNS throughput, RX 9060 XT

k8 FatTree (128 hosts), 128 flows at line rate (a permutation, cross-pod), HPCC:

| MTU | flow size | simulated | wall | frames/s |
|---|---|---|---|---|
| 1000 | 16 MB | 6.41 ms | 1.07 s | about 23 M |
| 4096 | 16 MB | 5.71 ms | 0.42 s | — |

That is about 170× slower than real time at 100 Gbps with MTU 1000, and about 75× with MTU 4096.
