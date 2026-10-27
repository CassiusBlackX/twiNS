#!/usr/bin/env python3
"""First difference between two queue histories (TwiNS --qtrace, twins-ref TRACE_ALLQ).
Records: ts node dev src dst kind seq-or-ack qlen-after, compared after a stable sort by
(ts, node, dev), which keeps each queue's enqueue order."""
import sys


def load(p):
    rows = []
    for l in open(p):
        r = l.split()
        if r and r[0] == 'Q':
            r = r[1:]
        if len(r) >= 8:
            rows.append((int(r[0]), int(r[1]), int(r[2])) + tuple(r[3:8]))
    return sorted(rows, key=lambda r: (r[0], r[1], r[2]))


a, b = load(sys.argv[1]), load(sys.argv[2])
print(f"{len(a)} vs {len(b)} records")
for i, (x, y) in enumerate(zip(a, b)):
    if x != y:
        print("first difference at record", i)
        for j in range(max(0, i - 3), min(i + 6, len(a), len(b))):
            print(f"  ref {a[j]}  |  sim {b[j]}{'   <<' if a[j] != b[j] else ''}")
        break
else:
    print("identical over the common prefix")
