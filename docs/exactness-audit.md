# Exact-or-flag: the comparators against the proof (audit, 2026-11-14)

Branch `exact-audit`. Theorem 2 of the paper (exact-or-flag) needs Lemma A.2: *a chain comparison
that raises no flag returns the lineage order*. The lemma is about the comparators as they are
coded, so this audit went through every place where a chain loses information and asked whether
a verdict could be returned from lost information without a flag. Ten such places were found; all
are now either exact or flagged.

## What was silent before

| # | Where | What was lost | Before | Now |
|---|---|---|---|---|
| a | TCP `chain_lead` | timer leads quantized to 16 ns and saturated at about 983 us: RTO 1 ms, its backoffs and the 10 ms SYN timeout all had one code | silent | order-preserving code; exact for 10 us, 1 ms·2^n, 10 ms·2^n; equal lossy codes: flag |
| b | TCP `chain_cmp` | a chain truncated to 9 runs that ran out with all leads equal was decided by its root | silent | truncation bit in the root key; flag |
| c | TCP `chain_root` | root key (host + 1, connection & 0xFFFF): wrapped at 65,536 connections and at 32,767 hosts | silent | world-local 30-bit rank computed on the host |
| c' | TCP | a saturated run count (0xFFFF repetitions, a 95 MB back-to-back run) compared as if exact | silent | flag when a saturated run runs out |
| d | TCP host | two timers due at the same instant ran in active-list order | silent | ordered by their chains; undecided: flag |
| e | RDMA/AI `memo_get` | comparisons remembered by 64-bit hashes of the two lead sequences | silent on collision | removed (strict build) |
| f | RDMA/AI `ccmp`, `qcmp` | equal lead hashes with distinct roots decided by root rank | silent on collision | removed (strict build) |
| g | RDMA/AI `ccmp_walk` | walk memo keyed by a 62-bit hash of (tail node, count left) pairs | silent on collision | exact keys, written once (below) |
| h | RDMA/AI `ccode` | ordinals ≥ 31 saturate; two saturated equal codes were walked past, and an ordinal verdict from a newer level returned | silent | `ORD_UNKNOWN`: a deeper ordinal difference still decides, otherwise flag |
| i | RDMA/AI `ccode` | leads above 2^27 − 2 ns (134 ms; the comment said ps) saturated: compute delays of the system layer, timers | silent | order-preserving 27-bit code; exact below 67 ms (2^26 ns) and for any lead with at most 21 significant bits; equal lossy codes: flag |
| i' | AI host loop (`closed.h`) | a timer against a completion compared the timer's lead with a lead decoded from the code | — | compares codes (monotone); equal codes go to the chains |
| i'' | RDMA root ranks (`rmain.cu`) | start chains sorted by lead codes | — | equal lossy leads counted as undecided |

UB is out of scope (dropped from the paper).

## The encodings

**TCP (16-bit codes).** ScheduleNow 1, Send's 1 ps deferral 2, TransmitComplete `len` (< 2048),
arrival `2048 + len`, timers `4096 + 2b + x`. For a timer lead L, q = ⌊L / 1 µs⌋, b = q below 1024,
else q with its 11 leading bits (1024 buckets an octave, up to 2^38 µs); x = 0 iff L is the least
value of its bucket. Properties:
1. **Monotone:** L ≤ L' ⇒ code(L) ≤ code(L') (buckets are ordered; within one, the exact code is the
   least lead).
2. **Exact codes are injective:** code(L) = code(L') even ⇒ L = L'. Non-timer codes are injective by
   construction (a length determines the lead at 100 Gb/s and 1 µs links).
3. **Timers above the rest:** every timer of the model is armed at least 10 µs ahead; a lead under
   2 µs would not order above arrival codes (≤ 1.164 µs) and is counted as a flag.

**RDMA/AI (27-bit lead part).** Leads below 2^26 ps: lead + 1. Above: 21 leading bits kept (2^20
buckets an octave, octaves 26–50), code `2^26 + 1 + 2b + x`. Same two properties; saturation at
2^51 ps is lossy.

## The exact walk memo (strict build, default)

An entry remembers a walk state: the two tail nodes (64-bit positions, never reused) and the
repetitions left in each, with the verdict of a walk decided by a lead from there on. Tail nodes
are immutable, so that verdict is a function of the state. An entry is three words; w0 is claimed
by CAS, w1 written, then w2 (counts and verdict, nonzero) last after a fence. A reader accepts only
when w2 is set and all three match, so it cannot combine words of two writers; entries are written
once. Verdicts that depend on ordinals seen above the state, or on a lost tail, are never stored.

## Results (RX 9060 XT)

- TCP, `check_all.sh --no-sweeps`: **306 of 306 runs identical**. Flags with 64-byte frames in 39
  runs (234 ties), all of cause `trunc` (gap b). These were decided by root before; their outputs
  were right then, but nothing guaranteed it. twins128 resolves all but 3 runs, which stay flagged
  and are still identical: k8_a2a50s fin NewReno (27 ties) and CUBIC (81), both cause `timers`
  (gap d); k16_a2a30 spray NewReno (2, `trunc`).
- RDMA: jobs1 10, jobs2 29, jobs3 8, jobs_dctcp 41, jobs_dctcp_strand 1 (5 stranded flows as
  designed), jobs_topo 32, sweep_k8 batch 32: **all identical, no flag**.
- Strict vs hash memo, RDMA k=8 sweep (32 worlds): 3.83–4.00 s vs 3.83–3.84 s.
- AI closed loop, strict build: c_allreduce_64m/512m_hpn128, c_allgather_512m_hpn128/1024 identical
  (messages and DAG lines), at the hash build's speed (13.3 vs 12.2 s; 21.3 vs 20.1 s).
  **LLaMA 65B probe (128 GPUs): over 300 s strict against 52 s with the hash memo.** Profile
  (`TWINS_STOP_SYNCS`, `RDMA_PROF`): in the hash build 21.6 M of 28.5 M comparisons were decided by
  "equal lead hashes, distinct roots"; lineages of distinct flows that are identical for thousands of
  runs. Exact interning of lead sequences (tail nodes carry the identity of the leads from them on,
  verified on the full key) works but cannot hold a training run in bounded memory: 9.6 M new
  identities in the first 300 of about 17,000 rounds, and one identity that does not fit makes every
  identity above it unique. Open: a fingerprint with a proven bound (polynomial hashes with random
  bases: Schwartz–Zippel), or state AI training under the hash memo with its bound.

## The uid oracle (validation/audit/oracle.cc)

`validation/audit/ns3_evlog_patch.py` patches ns-3.46.1 (Unison-for-ns-3 c92e91f) so that
`NS3_EVLOG=FILE` writes a 24-byte record at every Schedule (uid, parent uid, context, time) and
every executed event; unset, outputs equal the stored references (checked on k8_paper DCTCP, with and
without the log). The oracle rebuilds every event's full lineage and, for every pair of events run at
one timestamp (all pairs of groups up to 256 events, else neighbours plus as many random pairs):
Definition 1 on full lineages against ns-3's execution order (Theorem 1), and TwiNS's TCP comparator
(`src/chain_tcp.h`, the GPU's code) on the chains TwiNS would build from them (Lemma A.2).

132 runs (`validation/audit/oracle_jobs.txt`: the six FatTree k=8 workloads × 3 TCPs, base and
jittered; spraying; persistent connections with FIN; Jellyfish, torus, Dragonfly; link failures),
run on the 9950X, results in `out/audit/oracle/res`:

| | pairs | Theorem 1 disagrees | comparator right | undecided | wrong |
|---|---|---|---|---|---|
| all pairs | 1,259,008,010 | 0 | 1,205,157,858 (95.7%) | 53,849,875 | **0** |
| same node | 753,050,031 | 0 | | 53,462,475 | **0** |
| comparator before the audit, all pairs | | | 1,205,556,054 | 53,450,987 | **692** (17 runs) |
| ... same node | | | | | **4** |

1.33 billion events in all; the largest uid was 23 million (no run near the 2^32 wrap). 2,496 leads
had no TwiNS TCP code (topology files with other link delays and failures; the comparator treats
them as lossy). TwiNS compares only events of one port or host, so the undecided share here (all
simultaneous pairs) is far above what runs flag.

## Negative control (validation/audit/negctl_tcp.sh)

The 132 base and jittered TCP runs (14 workloads × 3 TCPs, k=8 × 5 jitters), with no twins128 rerun:

| chain runs | runs flagged | runs that differ from ns-3 | differ without a flag |
|---|---|---|---|
| 1 (build-r1) | 132 | 132 | 0 |
| 5 (build-r5) | 86 | 55 | 0 |
| 9 (default) | 23 | 6 | 0 |

Every run whose output differs is flagged; flags are conservative (17–31 flagged runs are still
identical).

## Internal state, not only outputs: every enqueue (out/audit/qdiff_k8.txt)

TwiNS `--qtrace` against ns-3 `TRACE_ALLQ` (every enqueue into every queue: time in ps, node,
device, flow, kind, sequence, queue length after), `validation/tools/qdiff.py`, on k8_paper,
k8_a2a30, k8_incast16 and k8_a2a50s × DCTCP, NewReno, CUBIC (twins128 where the default build flags
a tie): **12 of 12 identical**, 34.3 million enqueues, up to the last flow's completion. ns-3's
trace goes on with 13–292 pure ACKs after that (TwiNS stops when every flow has completed).

## Controlled tie order (validation/audit/tie_stats.py, out/audit/tie)

The same ns-3 binary (`NS3_TIE_SEED`): seed 0 reproduces every stored reference; seeds 1–10 run
simultaneous events in a seeded random order, nothing else changed. k=8, six workloads × three TCPs:
66–100% of flows change completion time; mean FCT −2.4..+1.7% (DCTCP), −12.5..+11.4% (NewReno),
−17.3..+24.9% (CUBIC); p99 −52.5..+650%; the largest per-flow change is a flow that waits out a
timeout in one order and not in another. k=16 a2a30: 94–95% of flows change.

## Cost of exactness (out/audit/cost_tcp.txt)

Same engine with `NS_NO_CHAIN` (no lineages; ties in arbitrary order), RX, min of 3: chains cost
1.02–1.14× (e.g. k16 a2a30 DCTCP 0.708 vs 0.646 s). Without them 0–16% of flows equal ns-3 (k16
paper: 0 of 1,024), and drops and retransmissions change (k16 a2a30 NewReno: 217,550 vs 167,211
retransmissions), so part of a time difference is different work. (The RDMA ablation build crashes in
host set-up, which assumes chains with roots; not pursued.)

## Correction (later the same day): the lead-identity shortcut, and tail-node reads

- The 18 SimAI collective runs at 128 and 512 GPUs with the lead-identity shortcut (interned lead
  sequences, "equal remainders and distinct roots → root order"): **10 of 18 wrong** (flows lost,
  "late events 16"), with no flag. A build that re-walks on every shortcut (TWINS_LSID_AUDIT)
  was right, so the cause was not found. The shortcut and the interning are now opt-in
  (`TWINS_LSID`), and tail nodes are back to 24 bytes.
- Tail nodes are now published and read as a seqlock on the GPU (pos cleared, fence, fields, fence,
  pos; readers check pos before and after volatile loads). Before, a block could read a node that
  another block wrote in the same launch with a stale field and a fresh pos.
- **Default strict build, final:** 18 of 18 collectives identical (messages and DAG lines);
  RDMA jobs1 10, jobs2 29, jobs3 8, jobs_dctcp 41, jobs_topo 32, sweep 32 worlds identical. Speed
  against the hash build: 1.10–1.25× (allreduce 512 MB at 128 GPUs 13.8 vs 12.0 s; all-gather
  512 MB at 512 GPUs 12.6 vs 10.1 s); the seqlock is about 1.06–1.15× of that.
- LLaMA 65B probe (training) is still over 400 s strict (52 s hash): open.
- RTX 5090 (CUDA, the build before these two changes): TCP 306/306, RDMA 120/120, sweep 32/32.

## Second round (night of 2026-11-14)

### Lead fingerprints for AI training (TODOS R-1.8, option A)
Strict mode was exact but slow on training (LLaMA 65B probe at 128 GPUs: more than 400 s against 52 s
for the old hash build), because lineages of distinct flows agree in every lead for thousands of
runs and the walk follows them to their roots. A chain now carries two polynomial fingerprints of
its lead codes after the root's set-up, `F_x = sum lambda_i x^(n-i) mod 2^61 - 1` (codes >= 1), for
two bases drawn independently and uniformly from [2, p - 1] at start-up (rejection sampling from the
OS; `--fp-bases=X1,X2` repeats a run; islands and reruns get the same bases). They decide one case
only: chains of distinct roots with equal fingerprints and no lossy code are ordered by their roots,
which is what the walk would find. Two different sequences of at most n codes are different
polynomials of degree < n, so one check of them passes with probability at most ((n-1)/(p-2))^2
(Schwartz-Zippel, two independent bases); a run prints the number of checks C, the longest lineage
n and the union bound C ((n-1)/(p-2))^2. Chains lost their `anc[]` hashes (strict build): 24 bytes
smaller. `TWINS_NO_FP` removes the fingerprints; `TWINS_HASH_MEMO` still builds the old version.
TCP has no fingerprints.
- LLaMA 65B probe, 128 GPUs: 55.0 s, 439,168 messages identical, 0 undecided, 111 M decisions,
  lineages up to 1.38 M events, bound 4e-17 (counted over decisions in that build; checks now).
- RX gate (`out/audit/gate_fp.txt`): AI collectives 18/18; RDMA jobs1 10/10, jobs2 29/29, jobs3 8/8,
  jobs_dctcp 41/41, jobs_topo 32/32, sweep 32/32 worlds; SimAI network 60/60; replay 16/16; what-if
  matrix 47/47; paper runs (closed loop) and both with 4 islands: see the file.

### HPCC-fork uid oracle (R-1.5): `validation/audit/hpcc_evlog_patch.py`, `oracle LOG rdma`
The HPCC fork (ns-3.18) patched to log every Schedule and executed event (outputs unchanged: every
run's FCTs equal the stored reference). The log streams through a FIFO into the oracle, which checks
Theorem 1 on full lineages and TwiNS-RDMA's comparator (27-bit lead codes, 5-bit saturating
ordinals, the deepest ordinal difference where lineages meet, roots in set-up order) on every pair of
simultaneous events (all pairs of groups up to 256, else neighbours and random pairs). 70 runs of
jobs1-3, jobs_dctcp and small topologies (the 1,024-host ones left out for the oracle's memory):
365 M events, 2.00 billion pairs (47.7 M at one node); Theorem 1 disagrees 0; the comparator is right
on every pair, 0 undecided, 0 wrong. (`validation/audit/oracle_rdma.sh`, a87 `~/netsim/rdma/oracle`.)

### Vanilla ns-3.46.1 (R-2)
Our TCP reference is ns-3.46.1 from Unison's tree run sequentially. Vanilla ns-3.46.1 has no per-flow
ECMP: `Ipv4GlobalRouting::FlowEcmpRouting` (5-tuple hash modulo the equal-cost routes) is Unison's
addition; the rest of Unison's changes are for thread safety. Results (a87):
- vanilla + `validation/ns3/flow-ecmp.patch` (Unison's change only) + our routing index patch:
  37/37 reference runs identical (k=8 six workloads x 3 TCPs, spraying, failures, persistent/FIN,
  six topology files, CDF workloads);
- vanilla with no patch at all: 21/21 spraying runs identical (so our patch's ordering of equal-cost
  routes by interface does not change them).
`twins-ref.cc` now asks for FlowEcmpRouting with SetDefaultFailSafe and stops only if --flowEcmp=1
cannot be had.

### Randomized grid (P0-7): `validation/grid/gen_grid.py 200 2027`, `check_grid.sh`
200 scenarios drawn at random: FatTree k=4/8, leaf-spine, Jellyfish, 2-D/3-D tori, Dragonfly, HyperX,
Xpander (16-320 hosts); all-to-all (web search, Hadoop, data mining, exp, uniform, fixed sizes; 20-60%
load), incasts, pairs; persistent connections; DCTCP/NewReno/Cubic; queue 50-400 and K; spraying,
FIN, link failures. 96,697 flows, ns-3 references on a87 (469 s on 20 cores).
- First pass: 199 of 200 identical; **g188 differed in 467 of 558 flows with no flag** (3-D HyperX,
  DCTCP, spraying, FIN). Cause, found with queue-trace diffs and ns-3's own DCTCP log: in ns-3 a
  connection's socket is forked from the host's listening socket and copies its `m_ecnCESeq`; the
  listener's CE check runs on every segment that reaches it, including segments of connections whose
  endpoint is already gone (spraying makes retransmissions arrive after the close), so the listener's
  value grows and every later connection ignores CE marks below it. TwiNS started each connection at
  -1. Fixed (`HostSoA::lst_ce`, `listener_ecn`): a handler-fidelity defect, which no flag can catch.
- After the fix: 200 of 200 runs identical, 96,697 of 96,697 flows; 18 reran with twins128; 2 keep a
  flag (undecided ties) and are identical anyway. check_all 306/306 still identical.
- (ns-3's debug build asserts `m_congState != CA_DISORDER` in SendPendingData on g188 at 510.6 us; the
  release reference runs without asserts.)

### Unison on one thread
The same binary with `--threads=1` against sequential ns-3 on the 9950X: 85-100% of flows differ on
all 16 runs (k=8/16, four workloads, DCTCP/NewReno); mean FCT -6.6 .. +13.5%.

### Lookahead sensitivity (P0-6): `validation/hpcc/jobs_delay.txt`
Uniform link delay 0.1-10 us on k=8 (a2a30 DCQCN and HPCC, incast DCQCN), references from the HPCC
fork (about 18 s each). Identical up to 1 us for all; DCQCN above 1 us is refused (its timer
intervals must be at least the slot), HPCC at 5 and 10 us first overflowed a link's frames per slot
(256, flagged); `--fcap` and an automatic rerun with 4x on a frame overflow fix it (identical).

### CUDA
`TailNode::pos` is uint64_t (unsigned long): CUDA's atomics need unsigned long long*; the seqlock
code now casts (HIP accepted it, so the seqlock had not been compiled for CUDA before).
