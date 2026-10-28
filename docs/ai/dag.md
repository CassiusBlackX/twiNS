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

## Traced DAGs (phase 3)

A SimAI run records its astra-sim actions (`validation/simai/trace_patch.py`, `TWINS_TRACE`):
- Every astra-sim action runs in an ns-3 event of one of four kinds:
  - QpComplete of flow f (`qp_finish`);
  - the SendComplete that completes f's send (`send_finish`);
  - a timer firing (`sim_schedule`);
  - main, before `Simulator::Run`.
- Each action is a `SendFlow` (one per queue pair) or a `sim_schedule`.
- An action's ordinal is ns-3's uid counter minus the event's uid at its start. Before the run it is
  the uid itself.
- In the runs so far:
  - every schedule inside a callback is a recorded action;
  - every callback starts at ordinal 0 (QpComplete precedes ReceiveAck's own scheduling).

`workloads/trace2dag.py` turns the trace into a DAG:
- `flow ID SRC DST SIZE`;
- `timer ID DELAY_NS`;
- one `on CHILD CAUSE ORD` per flow (`f<id>`) and timer (`t<id>`), where CAUSE is `r<flow>`,
  `s<flow>`, `t<timer>` or `m`. ORD is the ordinal within the cause's callback (for `m`: the uid).

It also turns SimAI's `fct.txt` into a reference per message (by source, destination and source
port).

The semantics, as in SimAI:
- A cause's actions run in ORD order inside its event.
- A flow is installed at the event's time t (Initialize: chain `E ⊕ (0, base + ORD)`) and starts
  at t + send_lat (`⊕ (send_lat, 0)`).
- A timer fires DELAY later with chain `E ⊕ (DELAY, base + ORD)`, and its own actions follow from
  ordinal 0.
- The event's later events follow its callback's ordinals. `h_ack` adds `dag_fire`'s count to its
  ordinal, which also applies to untraced DAGs.
- Main's actions are the roots, in uid order (root i + 1). Their chain is their own event: a
  timer's delay, or a flow's Initialize.

**TwiNS:**
- Timers touch only the DAG, so `dag_fire` expands them: a flow under a chain of timers gets the
  composed time and chain at once.
- Its message is filed send_lat after the firing event, not its start. The host keeps the flow
  ready until its start time (dyn list, wake-up).
- What main and its timers install is expanded on the host and loaded as ready flows.
- Quiet NVLink windows also wait for a server's fired NVLink flows that haven't started (`srv_fpend`).

**References:**
- `simai-dag-ref` reads the same DAGs.
- `simai-trace` (simai-ref's tree, instrumented) is SimAI end to end. Like simai-ref, it orders the
  frontend's node maps by node id. SimAI keys them by `Ptr<Node>`, which reorders ECMP next hops
  with the heap layout.
- SimAI's `ReadConf` forces `global_t = 1` (maxBdp and maxRtt for every queue pair) whatever
  `GLOBAL_T` says. simai-ref and TwiNS now do the same (`GLOBAL_T_HONOR 1`: as GLOBAL_T says).
