#!/usr/bin/env python3
"""Message DAGs for twins-rdma and simai-dag-ref (docs/ai/dag.md): "flow ID SRC DST SIZE",
"root ID START_NS", "dep ID KIND PARENT" (KIND r: PARENT received, s: PARENT sent).

usage: gen_dag.py ring GPUS BYTES [CHANNELS] > F     ring all-reduce over GPUS (a list a-b or a,b,..)
       gen_dag.py a2a GPUS BYTES PHASES > F          all-to-all in phases
       gen_dag.py pipe GPUS BYTES MICROBATCHES > F   pipeline stages GPUS[0] -> GPUS[1] -> ...
       gen_dag.py rails NGPUS GPS BYTES > F          a ring all-reduce per rail (GPU index within its
                                                     server) over all servers, side by side
The ring sends BYTES / len(GPUS) per step and channel, 2 (n - 1) steps: step k of rank i waits for
step k - 1 of rank i - 1 (received) and its own step k - 1 (sent). An all-to-all phase p sends
from i to i + p + 1; phase p + 1 of rank i waits for its phase-p message received and the phase-p
message to it received. A pipeline's microbatch m at stage s waits for m at s - 1 and m - 1 at s
(received).
"""
import sys


def gpus(spec):
    out = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out += list(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


class Dag:
    def __init__(self):
        self.flows, self.roots, self.deps = [], [], []

    def flow(self, src, dst, size):
        self.flows.append((src, dst, size))
        return len(self.flows) - 1

    def write(self):
        for i, (s, d, z) in enumerate(self.flows):
            print("flow", i, s, d, z)
        for i, t in self.roots:
            print("root", i, t)
        for i, k, p in self.deps:
            print("dep", i, k, p)


def ring(g, size, channels=1, t0=1000):
    n = len(g)
    dag = Dag()
    chunk = max(1, size // n)
    for c in range(channels):
        prev = None
        for k in range(2 * (n - 1)):
            cur = [dag.flow(g[i], g[(i + 1) % n], chunk) for i in range(n)]
            for i in range(n):
                if prev is None:
                    dag.roots.append((cur[i], t0))
                else:
                    dag.deps.append((cur[i], "r", prev[(i - 1) % n]))
                    dag.deps.append((cur[i], "s", prev[i]))
            prev = cur
    return dag


def a2a(g, size, phases, t0=1000):
    n = len(g)
    dag = Dag()
    prev = None
    for p in range(phases):
        sh = p % (n - 1) + 1
        cur = [dag.flow(g[i], g[(i + sh) % n], size) for i in range(n)]
        for i in range(n):
            if prev is None:
                dag.roots.append((cur[i], t0))
            else:
                psh = (p - 1) % (n - 1) + 1
                dag.deps.append((cur[i], "r", prev[i]))            # mine, received
                dag.deps.append((cur[i], "r", prev[(i - psh) % n]))  # the one to me, received
        prev = cur
    return dag


def pipe(g, size, mb, t0=1000):
    dag = Dag()
    ids = {}
    for m in range(mb):
        for s in range(len(g) - 1):
            f = dag.flow(g[s], g[s + 1], size)
            ids[(m, s)] = f
            if m == 0 and s == 0:
                dag.roots.append((f, t0))
            if s > 0:
                dag.deps.append((f, "r", ids[(m, s - 1)]))
            if m > 0:
                dag.deps.append((f, "r", ids[(m - 1, s)]))
    return dag


def rails(n, gps, size, t0=1000):
    dag = Dag()
    for r in range(gps):
        d = ring(list(range(r, n, gps)), size, 1, t0)
        base = len(dag.flows)
        dag.flows += d.flows
        dag.roots += [(i + base, t) for i, t in d.roots]
        dag.deps += [(i + base, k, p + base) for i, k, p in d.deps]
    return dag


def main():
    kind = sys.argv[1]
    if kind == "rails":
        rails(int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])).write()
        return
    g = gpus(sys.argv[2])
    size = int(sys.argv[3])
    if kind == "ring":
        d = ring(g, size, int(sys.argv[4]) if len(sys.argv) > 4 else 1)
    elif kind == "a2a":
        d = a2a(g, size, int(sys.argv[4]))
    elif kind == "pipe":
        d = pipe(g, size, int(sys.argv[4]))
    else:
        sys.exit("unknown kind " + kind)
    d.write()


if __name__ == "__main__":
    main()
