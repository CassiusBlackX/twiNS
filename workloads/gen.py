#!/usr/bin/env python3
"""Workload generator for TwiNS flow files (also readable by GeDES and the ns-3 scenario).

Output CSV (--pattern pair): node,flow_size,start_ns. GeDES pairs host i with host
i + N/2 (N = k^3/4) over one TCP connection, so a workload chooses, per pair, whether
each direction carries flows, their sizes and start times.

Output CSV (--pattern alltoall / incast): src,dst,flow_size,start_ns, with any number of
connections per host (GeDES cannot run these).
"""
import argparse
import bisect
import math
import random

MSS = 1460

# DCTCP web-search flow size CDF (bytes), as used by pFabric / HPCC.
WEBSEARCH_CDF = [
    (0, 0.0), (10000, 0.15), (20000, 0.2), (30000, 0.3), (50000, 0.4),
    (80000, 0.53), (200000, 0.6), (1000000, 0.7), (2000000, 0.8),
    (5000000, 0.9), (10000000, 0.97), (30000000, 1.0),
]


def sample_cdf(rng, cdf):
    u = rng.random()
    probs = [p for _, p in cdf]
    i = bisect.bisect_left(probs, u)
    i = max(1, min(i, len(cdf) - 1))
    (s0, p0), (s1, p1) = cdf[i - 1], cdf[i]
    if p1 == p0:
        return s1
    return s0 + (s1 - s0) * (u - p0) / (p1 - p0)


def flow_size(rng, args):
    if args.size == "fixed":
        pkts = args.mean_pkts
    elif args.size == "exp":
        # same shape GeDES uses internally: min(Exp(mean)+1, 2*mean)
        pkts = min(int(rng.expovariate(1.0 / args.mean_pkts)) + 1, 2 * args.mean_pkts)
    elif args.size == "uniform":
        pkts = rng.randint(args.min_pkts, args.max_pkts)
    elif args.size == "websearch":
        if args.align == "byte":
            return max(1, int(sample_cdf(rng, WEBSEARCH_CDF)))
        pkts = max(1, math.ceil(sample_cdf(rng, WEBSEARCH_CDF) / MSS))
    else:
        raise ValueError(args.size)
    if args.align == "byte":
        return int(pkts) * MSS - rng.randrange(MSS)  # the last segment 1..MSS bytes
    return int(pkts) * MSS


def start_time(rng, args):
    if args.start == "zero":
        return 0
    if args.start == "uniform":
        return int(rng.uniform(0, args.spread_ns))
    if args.start == "exp":
        return int(rng.expovariate(1.0 / args.spread_ns))
    raise ValueError(args.start)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=0, help="FatTree k (k^3/4 hosts)")
    ap.add_argument("--hosts", type=int, default=0, help="host count for other topologies (instead of --k)")
    ap.add_argument("--active", type=float, default=1.0, help="fraction of host pairs carrying traffic")
    ap.add_argument("--bidir", type=int, default=1, help="1: both directions of an active pair send")
    ap.add_argument("--size", choices=["fixed", "exp", "websearch", "uniform"], default="exp")
    ap.add_argument("--min_pkts", type=int, default=1028, help="uniform: smallest flow (packets), 1.5 MB")
    ap.add_argument("--max_pkts", type=int, default=4110, help="uniform: largest flow (packets), 6 MB")
    ap.add_argument("--mean_pkts", type=int, default=1000)
    ap.add_argument("--start", choices=["zero", "uniform", "exp"], default="exp")
    ap.add_argument("--spread_ns", type=int, default=1000000)
    ap.add_argument("--arrival", choices=["once", "poisson"], default="once",
                    help="once: one flow per sending host; poisson: back-to-back flows on each connection")
    ap.add_argument("--load", type=float, default=0.3, help="poisson: offered load per sending host (fraction of 100G)")
    ap.add_argument("--duration_ns", type=int, default=5000000, help="poisson: arrivals in [0, duration)")
    ap.add_argument("--pattern", choices=["pair", "alltoall", "incast"], default="pair",
                    help="alltoall: Poisson flows to uniform random destinations (at --load); "
                         "incast: rounds of --fanin synchronized senders to one receiver, "
                         "plus all-to-all background at --load (0 for none)")
    ap.add_argument("--fanin", type=int, default=16)
    ap.add_argument("--incast_bytes", type=int, default=64 * 1460)
    ap.add_argument("--incast_period_ns", type=int, default=100000)
    ap.add_argument("--max_peers", type=int, default=255, help="connections per host")
    ap.add_argument("--unique_pairs", type=int, default=0,
                    help="1: at most one flow per (src, dst), so per-pair and per-flow connections coincide")
    ap.add_argument("--align", choices=["mss", "byte"], default="mss",
                    help="flow sizes: MSS multiples, or any byte count (a partial last segment)")
    ap.add_argument("--peers", type=int, default=0,
                    help="all-to-all: each host sends to this many fixed random peers (0: any host)")
    ap.add_argument("--reuse", type=int, default=0,
                    help="1: the flows of a host pair share one connection (a 5th column, conn)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if not args.k and not args.hosts:
        ap.error("--k or --hosts")
    if args.pattern != "pair":
        return multi(args)

    rng = random.Random(args.seed)
    n = args.hosts or args.k ** 3 // 4
    half = n // 2
    pairs = list(range(half))
    rng.shuffle(pairs)
    n_active = max(1, round(args.active * half))
    active = sorted(pairs[:n_active])

    rows = []
    senders = []
    for i in active:
        senders.append(i)
        if args.bidir:
            senders.append(i + half)
    if args.arrival == "once":
        for h in senders:
            rows.append((h, flow_size(rng, args), start_time(rng, args)))
    else:
        mrng = random.Random(12345)
        mean_bytes = sum(flow_size(mrng, args) for _ in range(20000)) / 20000
        rate_per_ns = args.load * 100e9 / 8 / 1e9 / mean_bytes  # flows per ns
        for h in senders:
            t = rng.expovariate(rate_per_ns)
            n = 0
            while t < args.duration_ns and n < 99:  # GeDES flow queue (cap 100) must not wrap
                rows.append((h, flow_size(rng, args), int(t)))
                t += rng.expovariate(rate_per_ns)
                n += 1
    rows.sort()
    with open(args.out, "w") as f:
        f.write("node,flow_size,start_ns\n")
        for r in rows:
            f.write("%d,%d,%d\n" % r)


def multi(args):
    rng = random.Random(args.seed)
    n = args.hosts or args.k ** 3 // 4
    peers = [set() for _ in range(n)]
    per_pair = {}
    rows = []

    def add(src, dst, size, start):
        if (dst not in peers[src] and (len(peers[src]) >= args.max_peers or len(peers[dst]) >= args.max_peers)) or \
                per_pair.get((src, dst), 0) >= (1 if args.unique_pairs else 99):
            return False
        peers[src].add(dst)
        peers[dst].add(src)
        per_pair[(src, dst)] = per_pair.get((src, dst), 0) + 1
        rows.append((src, dst, size, start))
        return True

    if args.load > 0:
        mrng = random.Random(12345)
        mean_bytes = sum(flow_size(mrng, args) for _ in range(20000)) / 20000
        rate_per_ns = args.load * 100e9 / 8 / 1e9 / mean_bytes
        senders = [h for h in range(n) if rng.random() < args.active]
        for src in senders:
            t = rng.expovariate(rate_per_ns)
            fixed = rng.sample([h for h in range(n) if h != src], args.peers) if args.peers else None
            while t < args.duration_ns:
                for _ in range(8):  # retry a few destinations if peer limits are hit
                    if fixed:
                        dst = rng.choice(fixed)
                    else:
                        dst = rng.randrange(n - 1)
                        dst += dst >= src
                    if add(src, dst, flow_size(rng, args), int(t)):
                        break
                t += rng.expovariate(rate_per_ns)
    if args.pattern == "incast":
        size = args.incast_bytes if args.align == "byte" else (args.incast_bytes + MSS - 1) // MSS * MSS
        t = args.incast_period_ns // 2
        while t < args.duration_ns:
            r = rng.randrange(n)
            others = [h for h in range(n) if h != r]
            fresh = [h for h in others if (h, r) not in per_pair] if args.unique_pairs else others
            for src in rng.sample(fresh, min(args.fanin, len(fresh))):
                add(src, r, size, t)
            t += args.incast_period_ns
    rows.sort(key=lambda r: (r[0], r[3]))
    with open(args.out, "w") as f:
        if args.reuse:
            # one persistent connection per host pair; its flows are messages in start order
            f.write("src,dst,flow_size,start_ns,conn\n")
            for r in rows:
                f.write("%d,%d,%d,%d,%d\n" % (r + (r[0] * n + r[1],)))
        else:
            f.write("src,dst,flow_size,start_ns\n")
            for r in rows:
                f.write("%d,%d,%d,%d\n" % r)


if __name__ == "__main__":
    main()
