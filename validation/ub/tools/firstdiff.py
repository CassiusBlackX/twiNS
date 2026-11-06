#!/usr/bin/env python3
"""firstdiff.py A.port B.port [A.task B.task]: the earliest differing port event (by time) and task."""
import sys
def load(fn):
    d = {}
    for line in open(fn):
        n, p, t, ev, sz = line.split()
        d.setdefault((int(n), int(p)), []).append((int(t), ev, int(sz)))
    return d
a, b = load(sys.argv[1]), load(sys.argv[2])
best = None
for k in set(a) | set(b):
    x, y = a.get(k, []), b.get(k, [])
    for i in range(max(len(x), len(y))):
        u = x[i] if i < len(x) else None
        v = y[i] if i < len(y) else None
        if u != v:
            t = min(e[0] for e in (u, v) if e)
            if best is None or t < best[0]:
                best = (t, k, i, u, v, x[max(0, i - 3):i])
            break
if best:
    t, k, i, u, v, ctx = best
    print(f"first port diff at {t} ps node {k[0]} port {k[1]} index {i}: A={u} B={v}; before: {ctx}")
else:
    print("ports identical")
if len(sys.argv) > 4:
    ta = {l.split()[0] + ' ' + l.split()[1]: l.split()[3] for l in open(sys.argv[3])}
    tb = {l.split()[0] + ' ' + l.split()[1]: l.split()[3] for l in open(sys.argv[4])}
    ds = sorted((int(min(ta.get(k, '9e18'), tb.get(k, '9e18'), key=int)), k, ta.get(k), tb.get(k)) for k in set(ta) | set(tb) if ta.get(k) != tb.get(k))
    print(f"{len(ds)} task lines differ; first: {ds[:3]}")
