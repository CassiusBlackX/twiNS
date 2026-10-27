# Phase 2: dependency-driven traffic (design)

In SimAI, astra-sim reacts to completions by issuing sends:
- **Receive complete:** `QpComplete` at the sender. It runs in the ACK-arrival event at the
  flow's source GPU.
- **Send complete:** the first `SendComplete`. It runs in the `SendCallback` event, which the
  source GPU's `TransmitStart` scheduled `tx` after the frame's start.

Both events happen at the flow's **source** host. A send is `SendFlow`:
- `RdmaClientHelper::Install` on the source GPU. `Node::AddApplication` schedules
  `Application::Initialize` with delay 0, in that node's context. Its ordinal is k: the events the
  triggering event had scheduled before.
- `Initialize` schedules `StartApplication` after `send_lat` (ordinal 0).
- The source port is `portNumber[src][dst]++` at the call.

## The DAG

```
flow ID SRC DST SIZE                  a message (queue pair); ids 0 ..
root ID START_NS                      started at START_NS (by ScheduleFlowInputs, as simai-ref)
dep  ID KIND PARENT                   KIND r: PARENT received (QpComplete); s: PARENT sent (first SendComplete)
```

**Firing a flow.**
- A non-root flow starts when its last dependency fires. "Last" is in ns-3's order: the largest
  (time, chain) among the dependencies' events.
- It is installed in that event. Children installed by one event go in order of their ids, after
  what the event scheduled before its callback:
  - `QpComplete` comes before any scheduling in `ReceiveAck`;
  - `SendCallback` schedules nothing.
- Its `StartApplication` is at t + `send_lat`, with chain `E ⊕ (0, k) ⊕ (send_lat, 0)`.
- The events the triggering event schedules afterwards (`TriggerTransmit`'s) shift by the number
  of `Initialize` events it scheduled. Their ordinals have to follow.

## TwiNS

**Dependency messages.** A dependency event at host g.src sends a message (flow, time, chain) to
f.src. It arrives at time + `send_lat`, at least a slot later, so it is filed like a frame by
arrival slot (a per-host inbox).

**The source host:**
- counts each flow's dependencies, and keeps the latest (time, chain) by `before()`;
- on the last one, adds the flow's `StartApplication` to its start candidates;
- assigns source ports in the order of the installing events, which every one of its flows'
  messages carries. That keeps `portNumber[src][dst]++` exact.

**The `SendCallback` chain** is `cpush(ccode(tx, k), transmit event)`. The ordinal k is already
counted in `h_transmit`.

**Reference.** `validation/simai/simai-dag-ref.cc` is simai-ref with a DAG instead of a flow file,
with these semantics in `qp_finish` and `send_finish`.

## As implemented

**twins-rdma.** A flows file ending in `.dag` is read as a DAG.
- Roots start through `ScheduleFlowInputs` as before. They must share one start time, so their
  ports are taken before any completion.
- `dag_fire` (in `h_ack` at `QpComplete`, and in `h_transmit` at the first SendCallback) files a
  `DMsg` per child in the global bucket of the arrival slot, send_lat later. Each host takes its
  own flows' messages at the start of that slot.
- Ready flows are a sixth kind of candidate in the host's event selection. `h_start_dyn` takes the
  port (`pcnt`) and picks the devices (`PairNic`, `qp_pick`) at the start.

**The child's ordinal in the installing event** is taken as its index among the event's children
of that kind, as if every lower-id child were installed there too.
- Whether a sibling is also installed by that event is decided at the sibling's host, so the
  installing host cannot know it.
- Siblings keep their order, and the event's later events keep theirs (a constant shift).
- The approximation can only change a tie between chains with equal leads all the way down to a
  common root, which none of the DAG runs have shown.

**Phase 3** derives DAGs from astra-sim by instrumenting `sim_send` and ns-3's scheduler, which
gives the triggering event and k. The DAG semantics extend to astra-sim's own events (`sim_schedule`
compute delays) as needed.
