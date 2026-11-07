#!/usr/bin/env python3
"""nvl_islands.py FCT TOPO OUTDIR: the inputs of one twins-island per server, from twins-ai
--nvl-record's F.fct (id start fct src dst size: the flows between GPUs of one server) and the run's
SimAI topology. For each NVSwitch x with flows: OUTDIR/x.topo (its GPUs and itself, SimAI's format),
x.dag (its flows, local host ids, in global id order) and x.map (their global ids).
"""
import collections
import os
import sys

fct, topo, out = sys.argv[1:4]
os.makedirs(out, exist_ok=True)
lines = open(topo).read().split("\n")
head = lines[0].split()
nvs = [int(v) for v in lines[1].split()][: int(head[2])]
srv = {}
link = {}
for l in lines[2:]:
    x = l.split()
    if len(x) >= 4 and int(x[1]) in nvs and int(x[0]) not in nvs:
        srv[int(x[0])] = int(x[1])
        link[int(x[0])] = l
flows = collections.defaultdict(list)
for l in open(fct):
    g, st, f, a, b, sz = l.split()[:6]
    flows[srv[int(a)]].append((int(g), int(a), int(b), int(sz)))
for x, fl in sorted(flows.items()):
    gpus = sorted(h for h, v in srv.items() if v == x)
    lid = {h: i for i, h in enumerate(gpus)}
    n = len(gpus)
    with open(os.path.join(out, "%d.topo" % x), "w") as f:
        f.write("%d %d 1 0 %d %s\n" % (n + 1, n, n, head[5] if len(head) > 5 else "H100"))
        f.write("%d\n" % n)
        for h in gpus:
            y = link[h].split()
            f.write("%d %d %s\n" % (lid[h], n, " ".join(y[2:])))
    fl.sort()
    with open(os.path.join(out, "%d.dag" % x), "w") as f:
        for i, (g, a, b, sz) in enumerate(fl):
            f.write("flow %d %d %d %d\n" % (i, lid[a], lid[b], sz))
    with open(os.path.join(out, "%d.map" % x), "w") as f:
        f.write("\n".join(str(g) for g, _, _, _ in fl) + "\n")
print(len(flows), "islands,", sum(len(v) for v in flows.values()), "flows")
