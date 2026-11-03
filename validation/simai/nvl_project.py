#!/usr/bin/env python3
"""nvl_project.py EVFILE NSWITCH GPU_S OTHER_S: what running each server's NVLink part on CPU cores
would take (docs/ai/speed.md). EVFILE is twins-ai --nvl-record's .ev file: per sync, the slot time
reached and each switch's count of events inside its server since the sync before. Each window's
servers run in parallel on P cores (servers spread by their total, largest first), all of them
before the window ends: the window takes its busiest core's events x c, plus a barrier. GPU_S: the
GPU's time with the servers left out (--nvl-oracle); OTHER_S: system layer and sync.
"""
import struct
import sys

ev, nsw, gpu, other = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
rows = []
with open(ev, "rb") as f:
    while True:
        b = f.read(8 + 4 * nsw)
        if len(b) < 8 + 4 * nsw:
            break
        rows.append(struct.unpack("<q%dI" % nsw, b))
tot = [sum(r[1 + s] for r in rows) for s in range(nsw)]
srv = [s for s in range(nsw) if tot[s] > 0]
print("%d windows, %d servers, %.1f M events inside servers (%.1f M per server, largest %.1f M)" %
      (len(rows), len(srv), sum(tot) / 1e6, sum(tot) / len(srv) / 1e6, max(tot) / 1e6))
print("| cores | us per event | CPU, servers (s) | overlapped with the GPU (s) | one after the other (s) |")
print("|---|---|---|---|---|")
for P in (4, 8, 16, 32):
    order = sorted(srv, key=lambda s: -tot[s])
    load = [0] * P
    core = {}
    for s in order:
        i = load.index(min(load))
        core[s] = i
        load[i] += tot[s]
    for c in (0.2, 0.4, 0.8):
        barrier = 5e-6  # (a barrier of P threads per window)
        cpu = 0.0
        for r in rows:
            per = [0] * P
            for s in srv:
                per[core[s]] += r[1 + s]
            if max(per):
                cpu += max(per) * c * 1e-6 + barrier
        print("| %d | %.1f | %.1f | %.1f | %.1f |" % (P, c, cpu, max(cpu, gpu) + other, cpu + gpu + other))
