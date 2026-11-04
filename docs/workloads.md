# Real flow-size distributions

The workloads behind Unison's, HPCC's and GeDES's evaluations: Poisson arrivals, flow sizes drawn
from measured distributions, at a given share of each host's link. This page runs them on TwiNS
against ns-3 and GeDES, and gives the flow-completion-time slowdowns they produce.
`workloads/gen_cdf_eval.sh` makes every workload below.

## Result

- **Exact.** 46 of 46 runs identical per flow to sequential ns-3.46.1 (establishment and completion
  times): four distributions at 30, 50 and 70% load, DCTCP and NewReno, on FatTree k=8 and k=16,
  and the pair workloads GeDES runs.
- **TwiNS's slowdown curves are therefore ns-3's.** The tables below come from either; they are the
  same numbers.
- **Speed.** Against ns-3 on one core: 266–405× on web search at k=8, 789–1014× at k=16, up to
  1680× on Hadoop and 1967× on lunaWrite. DataMining is the exception, at 42–171×: a few flows of
  up to 1 GB keep the network busy long after the 10 ms of arrivals, and TwiNS's cost follows
  simulated time, not packets.
- **GeDES is fast but wrong.** On its own pairing (host i with i + N/2), TwiNS takes 0.44–7.2 s and
  GeDES 2.8–299 s. GeDES's mean flow completion time is 60–96% below ns-3's in 11 of 12 runs; its
  p99 slowdowns are 1.6–12 times too small for flows above 1 MB and up to 94 times too large for
  short ones under DCTCP. Its DCTCP and NewReno builds give the same flow times at k=16 and k=32.

## The workloads

`workloads/gen.py --size cdf --cdf FILE` draws flow sizes from a CDF file (sizes in bytes and
cumulative percent or fraction, linear between points) and spaces arrivals at each host as a
Poisson process at the load asked for. The CDFs, in `workloads/cdf/`, with their sources in each file:

| name | distribution | from | mean flow |
|---|---|---|---|
| ws | DCTCP web search (Alizadeh et al., SIGCOMM 2010) | HPCC's `traffic_gen` | 1.7 MB |
| hdp | Facebook Hadoop (Roy et al., SIGCOMM 2015) | HPCC's `traffic_gen` | 120 KB |
| luna | lunaWrite (Alibaba storage writes) | HPCC's `traffic_gen` | 41 KB |
| dm | VL2 data mining (Greenberg et al., SIGCOMM 2009) | HKUST-SING/TrafficGenerator | 12.7 MB |

- **All-to-all:** every host sends to random others, at 30, 50 and 70% of its 100 Gb/s. FatTree
  k=8 (128 hosts) over 10 ms of arrivals, k=16 (1024 hosts) over 2 ms.
- **Pairs, for GeDES:** host i sends to i + N/2, one connection each way, sizes rounded to whole
  packets (GeDES's flows are counted in packets), at 50% load. Web search over 10 ms (k=8) or
  2 ms (k=16, 32); Hadoop over 1.5 ms, since GeDES holds at most 99 flows on a connection.
- **Statistics** leave out the flows that arrive in the first 20% of the arrival window
  (warm-up).
- **Limits.** TwiNS's TCP model holds 256 active connections a host. NewReno at lunaWrite 70%, and
  at 50% on k=8, exceeds it (drops pile up connections behind 1 ms RTOs); TwiNS then says so
  (`active-ovf`), and those runs are left out.

## Exactness

`validation/check_exact.sh` against `data/ns3/ref`; ns-3 ran `validation/ns3/jobs_cdf.txt`.

| FatTree | workloads | TCPs | runs identical |
|---|---|---|---|
| k=8 | ws, hdp, dm at 30/50/70%; luna at 30% | DCTCP, NewReno | 20 of 20 |
| k=8 | luna 50%, 70% | DCTCP | 2 of 2 |
| k=16 | ws, hdp, dm, luna at 30/50/70% | DCTCP (and NewReno at 50%) | 16 of 16 |
| k=8, 16 | GeDES's pairs, ws and hdp at 50% | DCTCP, NewReno | 8 of 8 |

## Slowdowns

A flow's slowdown is its completion time over what it would take alone: the handshake's round
trip, then its bytes at 100 Gb/s behind one packet's store-and-forward on each hop of its path
(`validation/fct_slowdown.py`). p50 and p99 by flow size, FatTree k=16, 50% load:

| flow size | web search: flows | DCTCP p50 | p99 | NewReno p50 | p99 | Hadoop: flows | DCTCP p50 | p99 | NewReno p50 | p99 |
|---|---|---|---|---|---|---|---|---|---|---|
| <10 KB | 928 | 2.08 | 2.93 | 1.93 | 535 | 58113 | 2.04 | 3.08 | 1.68 | 538 |
| 10–100 KB | 2375 | 3.26 | 14.5 | 2.98 | 495 | 14847 | 5.16 | 174 | 6.00 | 500 |
| 100 KB–1 MB | 949 | 11.2 | 59.3 | 8.58 | 326 | 7513 | 26.9 | 167 | 39.8 | 374 |
| 1–10 MB | 1530 | 12.0 | 33.4 | 13.3 | 76.0 | 2042 | 20.2 | 56.6 | 22.1 | 98.5 |
| >10 MB | 172 | 5.76 | 12.0 | 6.84 | 14.3 | | | | | |

What they show, as in the DCTCP and HPCC papers: DCTCP keeps short flows within 3× of their ideal
at the 99th percentile; NewReno's queues overflow, and every short flow that loses a packet waits
out the 1 ms minimum RTO (p99 near 535×). Medium flows suffer most under both. lunaWrite at k=8,
30% (DCTCP p50/p99 by size: 2.22/3.18, 3.03/102, 16.9/201, 29.3/74.6) and DataMining (k=16 50%:
1.32/2.17 for short flows, 1.13/5.13 beyond 10 MB) follow the same pattern, the second much milder:
its load is in a few huge flows.

## Speed

TwiNS on the RX 9060 XT, its event loop, min of 3 runs. ns-3.46.1 (`validation/ns3/twins-ref.cc`)
on a87's Xeon Gold 5218, its event loop, 4–8 runs at a time (a87 was shared with another user's VM).

| FatTree | workload | load | flows | ns-3 DCTCP (s) | TwiNS (s) | × | ns-3 NewReno (s) | TwiNS (s) | × |
|---|---|---|---|---|---|---|---|---|---|
| k=8 | web search | 30% | 2703 | 374 | 1.40 | 266 | 460 | 1.48 | 310 |
| k=8 | web search | 50% | 4841 | 670 | 2.35 | 285 | 959 | 2.37 | 405 |
| k=8 | web search | 70% | 6502 | 929 | 3.06 | 304 | 1042 | 3.08 | 339 |
| k=8 | Hadoop | 30% | 38891 | 629 | 1.36 | 463 | 749 | 1.66 | 450 |
| k=8 | Hadoop | 50% | 64450 | 1577 | 2.30 | 687 | 1648 | 3.01 | 547 |
| k=8 | Hadoop | 70% | 90417 | 3207 | 3.33 | 963 | 3758 | 4.58 | 820 |
| k=8 | lunaWrite | 30% | 116401 | 1425 | 1.38 | 1030 | 1767 | 2.50 | 707 |
| k=8 | lunaWrite | 50% | 192775 | 4255 | 2.64 | 1612 | | | |
| k=8 | lunaWrite | 70% | 270343 | 8944 | 4.55 | 1967 | | | |
| k=8 | DataMining | 30% | 351 | 368 | 8.79 | 42 | 425 | 8.46 | 50 |
| k=8 | DataMining | 50% | 663 | 475 | 8.87 | 54 | 607 | 8.61 | 70 |
| k=8 | DataMining | 70% | 898 | 696 | 8.53 | 82 | 1156 | 8.41 | 137 |
| k=16 | web search | 30% | 4606 | 839 | 1.05 | 801 | | | |
| k=16 | web search | 50% | 7525 | 1235 | 1.57 | 789 | 1790 | 1.77 | 1014 |
| k=16 | web search | 70% | 10675 | 1818 | 1.98 | 917 | | | |
| k=16 | Hadoop | 30% | 62034 | 887 | 0.86 | 1031 | | | |
| k=16 | Hadoop | 50% | 103166 | 2109 | 1.30 | 1617 | 2029 | 1.98 | 1026 |
| k=16 | Hadoop | 70% | 144403 | 3041 | 1.81 | 1680 | | | |
| k=16 | lunaWrite | 30% | 183139 | 1411 | 0.93 | 1518 | | | |
| k=16 | lunaWrite | 50% | 295245 | 2932 | 1.85 | 1582 | 2911 | 4.15 | 701 |
| k=16 | lunaWrite | 70% | 406677 | 4320 | 4.51 | 959 | | | |
| k=16 | DataMining | 30% | 561 | 306 | 4.54 | 67 | | | |
| k=16 | DataMining | 50% | 1005 | 648 | 5.62 | 115 | 697 | 5.48 | 127 |
| k=16 | DataMining | 70% | 1405 | 1654 | 9.67 | 171 | | | |

Unison on these: see `docs/topologies.md`, whose `hdp30` workload is the Hadoop one at 30% on
every topology.

## GeDES

GeDES (`specsim-baseline` branch, `--flow_file`) builds FatTrees only, with one connection a host,
so it runs the pair workloads. Its time is its "Simulation Duration"; TwiNS's its event loop, min
of 2. Both on this machine (GeDES on the RX 9060 XT too). At k=32 there is no ns-3 reference
(8192 hosts); TwiNS's flows stand in for it, as they equal ns-3's wherever compared.

| FatTree | workload | TCP | flows | GeDES (s) | TwiNS (s) | GeDES mean FCT vs ns-3 |
|---|---|---|---|---|---|---|
| k=8 | web search | DCTCP | 4752 | 15.7 | 2.81 | −72.7% |
| k=8 | web search | NewReno | 4752 | 299 | 2.67 | −77.2% |
| k=8 | Hadoop | DCTCP | 9732 | 2.83 | 0.44 | +5.2% |
| k=8 | Hadoop | NewReno | 9732 | 2.92 | 0.59 | −86.2% |
| k=16 | web search | DCTCP | 7487 | 6.81 | 1.59 | −70.6% |
| k=16 | web search | NewReno | 7487 | 6.83 | 1.87 | −81.9% |
| k=16 | Hadoop | DCTCP | 76698 | 4.16 | 1.12 | −60.4% |
| k=16 | Hadoop | NewReno | 76698 | 4.10 | 1.71 | −91.9% |
| k=32 | web search | DCTCP | 59923 | 62.4 | 4.71 | −86.7% |
| k=32 | web search | NewReno | 59923 | 62.4 | 5.44 | −89.7% |
| k=32 | Hadoop | DCTCP | 614971 | 39.8 | 5.03 | −89.6% |
| k=32 | Hadoop | NewReno | 614971 | 38.4 | 7.18 | −95.9% |

Slowdowns, ns-3 against GeDES, FatTree k=16, 50% load, DCTCP (p50/p99):

| flow size | web search: ns-3 | GeDES | Hadoop: ns-3 | GeDES |
|---|---|---|---|---|
| <10 KB | 2.02 / 2.72 | 2.55 / 138 | 2.12 / 3.37 | 2.54 / 56.4 |
| 10–100 KB | 3.09 / 16.7 | 2.34 / 117 | 5.27 / 151 | 2.19 / 48.1 |
| 100 KB–1 MB | 12.0 / 55.8 | 1.73 / 66.5 | 25.6 / 166 | 1.56 / 29.3 |
| 1–10 MB | 12.9 / 27.0 | 1.20 / 17.2 | 17.0 / 46.3 | 1.18 / 7.99 |
| >10 MB | 5.72 / 11.4 | 1.10 / 3.55 | | |

GeDES lets large flows through almost as if alone (p50 1.1–1.2 where ns-3 has 6–17) and gives
short flows long tails that DCTCP does not produce. `validation/fct_slowdown.py K REF GEDES_OUT`
makes these tables (GeDES's address for host h is 192.168.0.0 + 48 × h's edge switch + h's
index under it).
