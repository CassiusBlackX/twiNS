#!/usr/bin/env python3
"""Summarise a parameter sweep (bench/sweep.py output, ns-3-format CSVs named
{workload}_{Tcp}_q{queue}_K{K}.csv): per configuration, the p99 FCT of short flows (< 100 KB)
and the mean FCT of long flows (> 1 MB), as tables over K x queue, plus the best K per
queue limit. With --ref, also counts configurations identical to the reference sweep."""
import argparse
import csv
import glob
import os
import re
import numpy as np


def load(p):
    rows = list(csv.DictReader(open(p)))
    size = np.array([int(r["flow_size"]) for r in rows])
    fct = np.array([int(r["completed_ns"]) - int(r["established_ns"]) for r in rows]) / 1e3
    return size, fct


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--ref", help="the same sweep from ns-3")
    a = ap.parse_args()
    cfg = {}
    for p in glob.glob(os.path.join(a.dir, "*_q*_K*.csv")):
        m = re.match(r"(.+)_(Tcp\w+)_q(\d+)_K(\d+)\.csv$", os.path.basename(p))
        cfg[(m.group(1), m.group(2), int(m.group(3)), int(m.group(4)))] = p
    if a.ref:
        same = sum(1 for key, p in cfg.items()
                   if open(p).read().splitlines() == [l[:len(l)] for l in open(os.path.join(a.ref, os.path.basename(p))).read().splitlines()]
                   or [l.split(",")[:6] for l in open(p).read().splitlines()] ==
                   [l.split(",")[:6] for l in open(os.path.join(a.ref, os.path.basename(p))).read().splitlines()])
        print(f"{same} of {len(cfg)} configurations identical to the reference\n")
    for w in sorted({k[0] for k in cfg}):
        for t in sorted({k[1] for k in cfg}):
            qs = sorted({k[2] for k in cfg if k[0] == w and k[1] == t})
            Ks = sorted({k[3] for k in cfg if k[0] == w and k[1] == t})
            print(f"== {w} {t}: short-flow p99 FCT (us) / long-flow mean FCT (us)")
            print("queue " + "".join(f"{'K=' + str(K):>14s}" for K in Ks))
            for q in qs:
                line, best = f"{q:5d} ", None
                for K in Ks:
                    p = cfg.get((w, t, q, K))
                    if not p:
                        line += f"{'-':>14s}"
                        continue
                    size, fct = load(p)
                    sp99 = np.quantile(fct[size < 100000], 0.99)
                    lmean = fct[size > 1000000].mean()
                    line += f"{sp99:7.0f}/{lmean:6.0f}"
                    if best is None or sp99 < best[0]:
                        best = (sp99, K)
                print(line + f"   best K for short flows: {best[1]}")
            print()


if __name__ == "__main__":
    main()
