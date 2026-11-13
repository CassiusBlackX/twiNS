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
| i | RDMA/AI `ccode` | leads above 2^27 − 2 ps (134 us) saturated: compute delays of the system layer, timers | silent | order-preserving 27-bit code; exact below 67 us and for any lead with at most 21 significant bits (1 ms, 55 us); equal lossy codes: flag |
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
