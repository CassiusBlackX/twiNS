# TwiNS-UB: UnifiedBus (ns-3-ub) simulated exactly

TwiNS-UB reproduces single-threaded ns-3-ub (OpenUSim, ns-3.44 UnifiedBus module, commit
6c28e38b) event for event: every per-port packet transmission and reception (PortTrace, ps) and
every task's start and completion (TaskTrace) are identical.

## What is modelled (base feature set)

- URMA_WRITE over RTP. One TP connection per host pair and priority, reserved in traffic.csv order
  (the TPN numbers follow ns-3-ub's reservation order).
- CBFC flow control: credit consumption at commit, piggybacked returns (4 cells, round robin over
  the VLs), and 40 B control frames once 1024 cells are pending.
- The SP round-robin allocator:
  - lazy RR phase seeding `(node + port) % slots`;
  - prefetch while the port is busy;
  - run-again;
  - `AllocationTime` per round.
- Store-and-forward ports with ns-3's exact tx times (`ubtime.h`: int64x64 Q64.64, checked on 135,544
  values).
- Routing: per-flow shortest paths with HASH64 (`ubhash.h`: ns-3 Hash64, checked).
- Transport (RTP):
  - 64 KB segments, 2 of them in flight per TP;
  - a TP ACK for every packet;
  - TA responses (TAACK) on the reverse TP.
- The transaction layer's RTP Tx queues, including the ScheduleNow chains (Prepare, Admit, RunRtpTx,
  target completion).
- The traffic generator:
  - phase dependencies;
  - a visibility delay of 100 ns;
  - CheckExampleProcess stopping the run at the first 100 µs boundary after the last task completes.
- ns-3-ub's default task cleanup (`EnableTaskScopedJettyDestroy`, `EnablePlannedTpConnectionDestroy`,
  both true). After the last planned use, a TP connection is destroyed: the peer is frozen, the
  owner's endpoint drains, then the peer's. This removes TPs from the allocators and shifts their
  round-robin slots.

Anything outside this set stops the run with a message rather than being simulated wrongly.
Examples: CC, PFC, DWRR, spray/adaptive routing, READ/LDST/CTP, multi-port hosts.

## Programs

| binary | source | what |
|---|---|---|
| `twins-ubseq` | `src/ub/seq.cc` | sequential, events in ns-3's (time, uid) order. `--chains` also checks that chain order equals uid order |
| `twins-ubpar` | `src/ub/par.cc` | the slot-synchronous parallel algorithm, on one thread (reference for the others) |
| `twins-ubmt` | `src/ub/mt.cc` | the same algorithm on N CPU threads (`--threads=N`); results do not depend on N |
| `twins-ub` | `src/ub/gpu.cu` | the same algorithm on the GPU (HIP): one block per node |

All four share `src/ub/model.h`, which holds the state (flat arrays, no STL) and the handlers. Each
ns-3 Schedule call is one engine call.

## How exactness is kept in parallel (`par.cc`, `mt.cc`, `gpu.cu`)

- **Slots.** A slot is the shortest link delay (at most the 100 ns visibility delay). In a slot every
  node runs its own events in ns-3's order. That order comes from causal chains (`chain.h`, after
  twins-rdma: lead = delay, ordinal = place among the parent's Schedule calls). A link arrival is
  always at least one slot ahead.
- **Traffic generator.** It is a global object. It runs first in each slot, because its events are
  scheduled at least 100 ns ahead.
- **Zero-delay events.** They run from a per-node FIFO, after the instant's earlier-scheduled
  events.
- **The one zero-lookahead interaction.** When a connection's owner endpoint finalizes, ns-3 removes
  the peer's TP from the peer node's allocator in the same instant. This is applied after the slot.
  The peer's allocator decisions since that instant are replayed without the TP, and its round-robin
  index is repaired; if a replayed decision would differ, the run counts a conflict (exact-or-flag).
  So far: up to 177 repairs per run and no conflicts.
- **Oracle.** `twins-ubseq --chains` checked 17 M same-time event pairs over the 11 cases. The chain
  order agreed with ns-3's uid order in all of them, with no undecided ties.

## Validation (validation/ub)

- `jobs1.sh` builds the matrix: 11 Clos cases (32–128 hosts) plus t2.
  - Workloads: all-to-all, ring allreduce, incast with fixed and random sizes, random flows.
  - Variants: 2:1 oversubscription, 50 ns forwarding delay, 200 Gbps / 100 ns links.
- `run_ref.sh` runs ns-3-ub on the 9950X and normalizes its runlog (`tools/norm.py`).
- `check.sh` compares the results. Every engine is identical on all 12 cases: 12.2 M port events and
  every task line.
- `evlog.patch` (debug build only) logs ns-3's event tree, giving the uid and the parent of every
  event.

## Speed (2026-11-07)

Same machine (Ryzen 9 9950X), run time without set-up:

| case | ns-3-ub (1 thread) | twins-ubmt 1 thread | 4 threads | 16 threads |
|---|---|---|---|---|
| a32_32m (32 hosts, 992 tasks) | 6.97 s | 0.82 s (8.6x) | 0.53 s (13x) | 1.24 s |
| ar32 (ring allreduce) | 4.10 s | 2.34 s (1.8x) | 0.91 s (4.5x) | 1.18 s |
| a128_4m (128 hosts, 16 256 tasks) | 7.46 s | 0.80 s (9.3x) | 0.35 s (21x) | 0.48 s |
| inc128 | 4.99 s | 0.46 s (11x) | 0.24 s (21x) | 0.43 s |

These topologies are small (40–168 nodes, one barrier per 20 ns slot), so more threads stop paying
off.

**ns-3-ub MTP (a87, 8 threads)** is only 1.3–1.7x faster than its own single-threaded run on the
same machine, and it is not exact. On a128_4m, 541 of 32 512 task lines and 37 536 port events
differ from the sequential run; on a32_32m, 1 370 of 1 984 task lines differ. With 1 thread it
matches the sequential run.

**GPU (`twins-ub`, RX 9060 XT).** The GPU is exact but far slower than the CPU engine, at about
30–45 µs per event on one lane.
- A microbenchmark gives the reason: one lane needs 75–260 ns per dependent global load and about
  2.5 ns per dependent 64-bit operation. A UB event runs thousands of 64-bit operations and around
  100 dependent loads.
- Fixes already tried, none of which changed the cost much: an inline-timestamp heap, a zero-delay
  FIFO, no struct copies, read-only world copies, VOQ bitmasks.
- One lane per node cannot be fast here. What could work: batches of worlds (sweeps); switch ports
  on separate lanes with LDS-resident state; or CPU threads for the heavy nodes and the GPU only
  for very wide, light work.

## Pitfalls met

- **TP teardown.** ns-3-ub destroys TP connections after their last planned use by default. Leaving
  this out gave correct results on uniform all-to-all but wrong round-robin order as soon as hosts
  had uneven TP lifetimes.
- **Allocator reruns.** The allocator schedules a round (AllocationTime) even when it picks nothing,
  and it reruns after every commit if any ingress queue is non-empty. Both create events that order
  ties.
- **Lazy phase seeding.** RR phase seeding happens on the first selection of each (port, VL), with
  the slot count at that moment. Receiver TPs are created on their first packet, which changes the
  slot count.
- **Multi-threaded inboxes.** In `twins-ubmt`, a node must take its inbox by swapping it under the
  lock; neighbors keep appending meanwhile (always for later slots).
- **Local tool build.** The local Python 3.14 breaks ns-3-ub's `./ns3`. Use `~/.venvs/ub`
  (Python 3.12) for ns-3-ub-tools, and build the references on a87 or the 9950X.
