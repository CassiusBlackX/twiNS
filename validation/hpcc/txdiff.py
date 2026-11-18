#!/usr/bin/env python3
"""The first difference between two transmission traces (rdma-ref --txtrace, twins-rdma --txtrace).

Each line: ns node dev kind src dst sport seq bytes ecn. Records are compared in (ns, node, dev)
order; a link starts one transmission at a time, so that order is total.

usage: txdiff.py REF SIM [context]
"""
import sys


def load(path):
    rows = []
    with open(path) as f:
        for line in f:
            v = line.split()
            if len(v) not in (10, 14):
                raise ValueError("invalid transmission record: " + line)
            rows.append((*map(int, v[:3]), v[3], *map(int, v[4:])))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    return rows


def main():
    ref, sim = load(sys.argv[1]), load(sys.argv[2])
    ctx = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    print("%d vs %d records" % (len(ref), len(sim)))
    n = min(len(ref), len(sim))
    for i in range(n):
        if ref[i] != sim[i]:
            print("first difference at record %d" % i)
            for j in range(max(0, i - ctx), min(n, i + ctx + 1)):
                mark = "   <<" if ref[j] != sim[j] else ""
                print("  ref %-60s | sim %s%s" % (" ".join(map(str, ref[j])), " ".join(map(str, sim[j])), mark))
            return 1
    print("identical over the common prefix")
    return 0 if len(ref) == len(sim) else 1


if __name__ == "__main__":
    sys.exit(main())
