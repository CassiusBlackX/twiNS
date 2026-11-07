#!/usr/bin/env python3
"""fct_slowdown.py K REF [GEDES] [--warmup F]: FCT slowdown by flow size on a FatTree k (docs/workloads.md).

REF is an ns-3 output of validation/ns3/twins-ref.cc (src,dst,flow_size,start_ns,...,completed_ns);
TwiNS's outputs equal it flow for flow, so its numbers are TwiNS's too. GEDES, if given, is a GeDES
output of the same flows (source_ip,dst_ip,flow_size,start_timestamp,completed_timestamp,...).

A flow's ideal FCT is what it takes alone: the handshake's round trip, then its bytes at 100 Gb/s
behind one packet's store-and-forward per hop, over its path's links (2, 4 or 6 at 1 us each).
Flows that start in the first F of the arrivals (default 0.2) are left out (warm-up).
"""
import csv
import sys

BINS = [(0, 10e3, "<10KB"), (10e3, 100e3, "10KB-100KB"), (100e3, 1e6, "100KB-1MB"), (1e6, 10e6, "1MB-10MB"), (10e6, 1e18, ">10MB")]


def links(k, a, b):
    if a == b:
        return 0
    e = k // 2
    if a // e == b // e:
        return 2
    if a // (e * e) == b // (e * e):
        return 4
    return 6


def ideal(k, a, b, size):
    n = links(k, a, b)
    return 2 * n * 1000 + n * 1000 + (n - 1) * 1500 * 8 / 100 + size * 8 / 100


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(p * len(v)))] if v else float("nan")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    warm = 0.2
    for a in sys.argv[1:]:
        if a.startswith("--warmup="):
            warm = float(a.split("=")[1])
    k, ref = int(args[0]), args[1]
    rows = list(csv.DictReader(open(ref)))
    t_end = max(int(r["start_ns"]) for r in rows)
    t0 = warm * t_end
    ns = {}
    for r in rows:
        a, b, sz, st, done = int(r["src"]), int(r["dst"]), int(r["flow_size"]), int(r["start_ns"]), int(r["completed_ns"])
        if done > 0:
            ns[(a, b, sz, st)] = done - st
    gd = {}
    if len(args) > 2:
        for r in csv.DictReader(open(args[2])):
            # (GeDES's address of host h: 192.168.0.0 + 48 * (h's edge switch) + h's index under it)
            ip = lambda x: ((int(x) - 3232235520) // 48) * (k // 2) + (int(x) - 3232235520) % 48 if int(x) >= 3232235520 else int(x)
            gd[(ip(r["source_ip"]), ip(r["dst_ip"]), int(r["flow_size"]), int(r["start_timestamp"]))] = int(r["flow_completion_time"])
    print("| flow size | flows | p50 slowdown | p99 slowdown |" + (" GeDES p50 | GeDES p99 |" if gd else ""))
    print("|---|---|---|---|" + ("---|---|" if gd else ""))
    for lo, hi, name in BINS:
        sd, sg = [], []
        for (a, b, sz, st), f in ns.items():
            if st < t0 or not (lo <= sz < hi):
                continue
            sd.append(f / ideal(k, a, b, sz))
            g = gd.get((a, b, sz, st))
            if g:
                sg.append(g / ideal(k, a, b, sz))
        if not sd:
            continue
        line = "| %s | %d | %.2f | %.2f |" % (name, len(sd), pct(sd, 0.5), pct(sd, 0.99))
        if gd:
            line += " %s | %s |" % ("%.2f" % pct(sg, 0.5) if sg else "—", "%.2f" % pct(sg, 0.99) if sg else "—")
        print(line)
    if gd:
        both = [(f, gd[x]) for x, f in ns.items() if x in gd]
        if both:
            m_ns = sum(f for f, _ in both) / len(both)
            m_gd = sum(g for _, g in both) / len(both)
            print("mean FCT: ns-3 %.1f us, GeDES %.1f us (%+.1f%%), %d of %d flows matched" % (m_ns / 1e3, m_gd / 1e3, 100 * (m_gd - m_ns) / m_ns, len(both), len(ns)))


if __name__ == "__main__":
    main()
