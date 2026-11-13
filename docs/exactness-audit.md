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
