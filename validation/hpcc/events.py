#!/usr/bin/env python3
"""The events at one node in a time window, reconstructed from a transmission trace: frames
arriving at it (from its neighbours' transmissions), its transmission starts and their ends.

usage: events.py TOPO TRACE NODE FROM_NS UNTIL_NS [--rate=100e9 --delay=1000]
"""
import sys


def tx_ns(size, bps):
    # Seconds(size * 8 / bps) in ns-3.17: floor of the 64.64 fixed-point value times 10^9
    x = size * 8 / bps
    lo = int(x * 2.0 ** 64)
    return (lo * 1000000000) >> 64


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = dict(a[2:].split("=") for a in sys.argv[1:] if a.startswith("--"))
    bps, delay = float(opts.get("rate", 100e9)), int(opts.get("delay", 1000))
    topo, trace, node, t0, t1 = args[0], args[1], int(args[2]), int(args[3]), int(args[4])
    H, links = 0, []
    for line in open(topo):
        v = line.split()
        if v and v[0] == "hosts":
            H = int(v[1])
        elif v and v[0] == "link":
            links.append(tuple((0 if x[0] == "h" else H) + int(x[1:]) for x in v[1:3]))
    deg, peer = {}, {}
    for a, b in links:
        pa, pb = deg.get(a, 0) + 1, deg.get(b, 0) + 1
        deg[a], deg[b] = pa, pb
        peer[(a, pa)] = (b, pb)
        peer[(b, pb)] = (a, pa)
    ev = []
    for line in open(trace):
        v = line.split()
        t, n, d, size = int(v[0]), int(v[1]), int(v[2]), int(v[8])
        tx = tx_ns(size, bps)
        desc = " ".join(v[3:])
        if n == node:
            if t0 <= t <= t1:
                ev.append((t, "start  port %d  %s" % (d, desc)))
            if t0 <= t + tx <= t1:
                ev.append((t + tx, "end    port %d  %s" % (d, desc)))
        p = peer.get((n, d))
        if p and p[0] == node and t0 <= t + tx + delay <= t1:
            ev.append((t + tx + delay, "arrive port %d  %s  (sent %d)" % (p[1], desc, t)))
    for t, e in sorted(ev):
        print(t, e)


if __name__ == "__main__":
    main()
