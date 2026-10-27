#!/usr/bin/env python3
"""Is a simulator's error relative to ns-3 within ns-3's own run-to-run variability?

For every workload and TCP variant there are ns-3 runs on several variants of the flow file
(base, and start times jittered by 0-10 ns with different seeds). The noise floor is the spread
between pairs of ns-3 runs; the model error compares the simulator on variant v with ns-3 on the
same variant v. Distances: relative error of the mean FCT, relative error of the p99 FCT, and the
KS distance between the FCT distributions (the GeDES paper reports the mean error).

Files:
  --ref   {w}_{T}.csv, {w}_{T}_{v}.csv     ns-3 (T = TcpDctcp | TcpNewReno; default data/ns3/ref)
  --twins {w}_{t}_{v}.csv                  TwiNS (t = dctcp | newreno, v = base | jit...; default
                                           out/nsm, as written by validation/check_exact.sh)
  --gedes {w}_{t}.csv                      GeDES model on the base flows (GeDES CSV format;
                                           default data/gedes)
"""
import argparse
import itertools
import os
import numpy as np
from compare import load_ns3, load_gedes, ks

TCP = {"dctcp": "TcpDctcp", "newreno": "TcpNewReno"}


def fcts(d):
    return np.array([v for v in d.values() if not np.isnan(v)], dtype=float)


def dist(a, b):
    """distances of FCT sample a from reference b (distribution-level, no flow matching)"""
    a, b = fcts(a), fcts(b)
    return (abs(a.mean() - b.mean()) / b.mean(), abs(np.quantile(a, .99) - np.quantile(b, .99)) / np.quantile(b, .99),
            ks(a, b))


def fmt(xs):
    xs = np.array(xs)
    return f"{100 * np.median(xs[:, 0]):5.1f} {100 * xs[:, 0].max():5.1f} | {100 * np.median(xs[:, 1]):5.1f} | {np.median(xs[:, 2]):.2f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="data/ns3/ref")
    ap.add_argument("--twins", default="out/nsm")
    ap.add_argument("--gedes", default="data/gedes")
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--workloads", default="paper,fullsync,fullexp,a2a30,incast16,a2a50s")
    ap.add_argument("--variants", default="base,jit,jit1,jit2,jit3,jit4")
    a = ap.parse_args()
    vs = a.variants.split(",")
    print(f"{'':20s} {'ns-3 noise (pairs)':>26s}   {'TwiNS (same variant)':>26s}   {'GeDES model (base)':>26s}")
    print(f"{'workload':10s} {'tcp':8s}  " + "   ".join([f"{'mean med/max | p99 | KS':>26s}"] * 3))
    for w in a.workloads.split(","):
        wk = f"k{a.k}_{w}"
        for t in TCP:
            ref = {}
            for v in vs:
                p = f"{a.ref}/{wk}_{TCP[t]}.csv" if v == "base" else f"{a.ref}/{wk}_{TCP[t]}_{v}.csv"
                if os.path.exists(p):
                    ref[v] = load_ns3(p, True)
            if len(ref) < 2:
                continue
            noise = [dist(ref[x], ref[y]) for x, y in itertools.permutations(ref, 2)]
            model = [dist(load_ns3(f"{a.twins}/{wk}_{t}_{v}.csv", True), ref[v]) for v in ref
                     if os.path.exists(f"{a.twins}/{wk}_{t}_{v}.csv")]
            g = f"{a.gedes}/{wk}_{t}.csv"
            gedes = [dist(load_gedes(g, a.k), ref["base"])] if os.path.exists(g) and "base" in ref else []
            print(f"{w:10s} {t:8s}  {fmt(noise):>26s}   {fmt(model) if model else '-':>26s}   {fmt(gedes) if gedes else '-':>26s}")


if __name__ == "__main__":
    main()
