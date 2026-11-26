# Switch extension experiments

The 11/27 checklist asks for an independent reference for a stateful switch
mechanism and for TCP runs with more than 32 active ports. These experiments use
packet-network runs and retain the ordered events used to decide correctness.

## ConWeave receive VOQ

The selected mechanism is a receive ToR's reordering VOQ for one phase change
within an epoch. The experiment prescribes old and new source ports. The sender
marks a tail packet on the old path. New-path packets that arrive early are
queued in FIFO order. The old path can revise the deadline, the tail moves it to
one nanosecond after its arrival, and a missing tail permits a timer-driven
release. The original author RxToR pipeline and VOQ are the independent
reference. The fixture edits the author's TxToR policy, adds an explicit BDP for
the small topology, and logs state transitions. The supported scope is one
controlled IRN data QP. The original sender's adaptive path selection,
feedback replies, and epoch aging are outside this scope.

The `fixtures` directory contains the shared input and the author and TwiNS
topology encodings. The two paths have equal hop count and 8 µs versus 2 µs of
inter-switch propagation. The source port changes after data sequence 2,000.
The negative capacity run deliberately uses one VOQ entry.

```sh
python3 experiments/paper_switches/conweave_reference.py \
    /path/to/conweave-stock /path/to/conweave-routing.cc /path/to/irn-stock/main.cc \
    experiments/paper_switches/fixtures reference
python3 experiments/paper_switches/conweave_run.py run observed \
    --binary /path/to/twins-rdma --inputs experiments/paper_switches/fixtures --trace
python3 experiments/paper_switches/conweave_run.py compare observed --inputs reference
```

The packet RDMA graph accepts explicit `rate=100Gbps delay=4000ns` attributes
after a link's two endpoints. The controlled mode uses
`--conweave-voq=FLOW,TX_SWITCH,RX_SWITCH,OLD_DEVICE,NEW_DEVICE,TAIL_BYTE`. Devices
are one-based ns-3 interface numbers. Set `TWINS_CW_TRACE=1` to record changes to
the VOQ. The bound is set with `--conweave-voq-cap`. The mode chooses the existing
ordered switch path and drains to the exclusive cutoff to observe pending
events. Production forwarding, the NIC, packet sizes, and normal IRN recovery
are used. A too-small VOQ returns exit code 3. Process timing is measured in
separate invocations without packet or state tracing.

## Wide TCP

`wide_tcp.py generate` produces explicit Leaf–Spine graphs and immutable flow
tables. There are four leaves and 16 spines. The leaves have 16, 32, or 48 access
links, plus 16 uplinks. The 64-port topology also has a mixed 25/100 Gb/s and
nonintegral propagation variant. An all-to-all cohort uses the access links and a
second cohort converges on the highest numbered access interface. Both per-flow
ECMP and per-packet spraying are included. The timing and trace commands use a
common 2 ms cutoff; queue size 24 gives some expected incomplete flows. Queue
size 2,048 on the mixed case provides a separate full-completion control.

```sh
python3 experiments/paper_switches/wide_tcp.py generate inputs
python3 experiments/paper_switches/wide_tcp.py native reference \
    --binary /path/to/ns3-twins-ref --inputs inputs --trace
python3 experiments/paper_switches/wide_tcp.py gpu observed \
    --binary /path/to/twins512-links --inputs inputs --trace
python3 experiments/paper_switches/wide_tcp.py compare observed --binary reference
```

The trace comparison includes six TCP flow fields, the numerical route table,
and exact timestamps and order within each queue. It also counts how many
interfaces above 32 are active, their maximum queue occupancy, and groups where
different sources enqueue to the same high-numbered interface at the same
time. Use a separate output directory with `--trials 3` for timing, or append
`--case 64-mixed --queue 2048` to run the full-completion control.

The wide switch path assigns one port per lane in a 64-thread block. Existing
barriers separate ingress counting, routing, and egress merges. The single
spray pass consumes routing draws in the same order across all ports. Its
queue timing is the same as for the small blocks. The code supports a maximum
of 64 ports per graph or Leaf–Spine switch. FatTree retains its existing size
limit and parameterization.
