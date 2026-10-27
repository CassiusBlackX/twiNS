# ns-3-alibabacloud versus the HPCC fork: what twins-rdma must add

ns-3-alibabacloud (SimAI `a57d765`, submodule `3e0c7c1`) is the HPCC RDMA model ported to
ns-3.36.1. Below, every change in `src/point-to-point/model`, `network/utils` and the SimAI glue
(`astra-sim-alibabacloud/astra-sim/network_frontend/ns3`) that can change a result, with what
twins-rdma needs. Changes that only add monitoring, traces or type widening without effect are
listed at the end.

## Changes that alter frames and timing

| # | change | where | effect | twins-rdma |
|---|---|---|---|---|
| 1 | Sequence numbers are 64-bit (`CustomHeader`, `qbbHeader`, `SimpleSeqTsHeader`) | network/utils/custom-header, qbb-header, simple-seq-ts-header | data header and ACK/NACK are 4 bytes larger | header sizes become parameters |
| 2 | `CalculateBytesTxTime` instead of `Seconds(CalculateTxTime)` | qbb-net-device, rdma-hw (pacing, rate change) | ns-3.36 computes `Seconds(bytes*8) / m_bps` with the integral `Time / uint64` operator: exactly `floor(bytes*8e9 / bps)` ns. The fork truncated a double product, which is 1 ns short whenever the exact value is an integer the double misses (e.g. 1000 B at 100 Gb/s) | a second tx-time rule, integer exact; verify on the trace |
| 3 | Links have their own rate and delay (topology: `src dst rate delay err`) | SimAI topology | NVLink 2400–2880 Gb/s at 25 ns; NIC 100–400 Gb/s at 500 ns; uplinks 400 Gb/s | heterogeneous links (phase 1.1) |
| 4 | Per-port PFC parameters: `headroom = rate*delay/8e9*3`; `pfc_a_shift = 3`, decremented while the port rate exceeds the NIC rate | common.h `SetupNetwork` | thresholds differ per port | per-port headroom and shift |
| 5 | ECN thresholds by port rate: `KMIN/KMAX/PMAX_MAP` | common.h | marking per port rate | per-port Kmin, Kmax, Pmax |
| 6 | `GLOBAL_T 0`: window and base RTT per pair (`pairBdp`, `pairRtt` from BFS: `delay*2 + txDelay`, `txDelay` = one MTU frame per hop, `bw` = minimum on the path) | common.h | each QP's window and HPCC base RTT differ | per-QP `win`, `baseRtt` |
| 7 | MTU 9000 (`PACKET_PAYLOAD_SIZE`) | SimAI.conf | larger frames | tx table up to 9000 + headers |
| 8 | Finished-sending QPs count for the host's wake-up: the `GetBytesLeft() == 0` skip was removed from both `m_nextAvail` minimum loops in `DequeueAndTransmit` | qbb-net-device | a QP with all bytes sent but not yet acknowledged, and `m_nextAvail` in the past, makes the minimum `<= now`, so no wake-up is scheduled and the NIC idles until the next trigger (ACK, CNP, new QP) | host candidate times must include such QPs |
| 9 | Send completion event: for every frame a host transmits with `60 < size < 9000`, an event at `now + txTime` calls `SendPacketComplete`, which looks up the QP by (dip, sport, pg) and fires `SendComplete` | qbb-net-device `TransmitStart`, rdma-hw | one extra event per such frame; the sender learns "sent" from it | emit this event (phase 2, DAG) |
| 10 | Receive completion is the sender's `QpComplete` (last ACK at the sender), not data arrival at the receiver | entry.h `qp_finish` | message completion = sender's final ACK | DAG semantics |
| 11 | Flow start: `RdmaClient` installed at the call, started `send_lat` later; node adds the application with a 0-delay `Initialize` event, which schedules `Start` after `send_lat` | entry.h `SendFlow` | start = call time + `send_lat`, through two events | DAG start delay |
| 12 | `send_lat`: `AS_SEND_LAT` in µs, times 1000 = ns. Without the variable it comes from NCCL tables in ns and is still multiplied by 1000, i.e. ms (time resolution is the default ns) | entry.h | SimAI quirk; we always set `AS_SEND_LAT` | — |

## New node kinds

**NVSwitch node** (`NVSwitchNode`, node type 2), nvswitch-node.cc:
- ECMP like a switch.
- Ingress and egress admission in its own MMU, so it can drop.
- No PFC generation, no ECN marking, no INT.
- It also has an `RdmaHw` (a NIC) for NVLS: with `nvls_enable` it acts as a host (`SwitchAsHostSend`). Frames with ToS 4 addressed to it terminate there.

**GPU host**: an ordinary host node (type 0) with two devices:
- an NVLink port to its server's NVSwitch;
- a NIC port to the ToR.

`RdmaHw::GetNicIdxOfQp` picks the NVSwitch port when source and destination share a server
(`src / gpus_per_server`) or when the destination has an NVSwitch route; otherwise it picks the
NIC by `qp->GetHash() % n`.

**Routing**: `CalculateRoute` does a BFS from each host.
- Next hops via an NVSwitch replace the other next hops: when one is found, the list is cleared and only NVSwitch hops are kept.
- Hosts get an entry for each neighbouring host.

## Simulator kernel and determinism

- **Unison is always on.** The default build enables Unison (`--enable-mtp`), and `MtpInterface::Enable(threads)` runs even with `-t 1`.
  - Logical processes run in each round in an order sorted by measured wall-clock time (`ByExecutionTime`).
  - Astra-sim's state and the ECN stream counter are shared, so results change from run to run (`baseline.md`).
- **ECN** draws a new `UniformVariable` per marking decision. Its stream depends on how many were created before it anywhere (same as the HPCC fork). The fix: one stream per switch port, as `hpcc.patch` does.
- **Routes**: `nbr2if`, `nextHop`, `pairDelay`… are `map<Ptr<Node>, …>`, ordered by heap address. ECMP next-hop order follows allocation order. That is stable in practice, but must be ordered by node id.
- **Error model**: one shared `RateErrorModel` with rate 0 is attached to every device. It draws once per received frame and never corrupts, so it has no effect.

**The reference** is `simai-seq`: SimAI built without MTP (sequential `DefaultSimulatorImpl`), plus a patch for ECN streams and route order. The existing sequential-chain ordering of twins-rdma applies to it unchanged.

## Changes without effect on results

- **Monitors**: `PrintSwitchQlen`, `PrintSwitchBw`, `PrintHostBW`, `PrintQPRate` and `PrintQPCnpNumber` with their counters, `m_rdmaUpdateTxBytes` and `qp_cnp`.
  - They are scheduled from `schedule_monitor`, which `AstraSimNetwork.cc` does not call.
- **No-op code**: the no-op block in `GetNextQindex`; `TotalPauseTimes`.
- **Widened types**: `pCnt` 257→1025; egress bytes 64-bit.
- **Trace and type changes**: trace-source type names; `size_t` in channel.
- **MPI remote channels**: not built.
- **`SwitchDequeueAndTransmit`**: equals the switch branch of the old `DequeueAndTransmit`.

## Effort, in phase-1 order

1. Header sizes (1) and the tx-time check (2): small.
2. Heterogeneous links, per-port PFC, ECN and `pfc_shift` (3–5): the main work, as planned.
3. Per-QP window and base RTT (6): small; they move from `Params` into the QP.
4. MTU 9000 (7): parameters; sizes of the tx table and the frame slots.
5. Host wake-up with finished QPs (8): small, but it has to be exact.
6. NVSwitch and GPU-host routing: a new switch kind without PFC, ECN or INT. NVLS goes last.
7. Completion semantics (9–11): phase 2, part of the DAG engine.
