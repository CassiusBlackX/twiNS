#!/usr/bin/env python3
"""Compare per-flow FCTs of GeDES-model runs and ns-3-format runs (TwiNS, ns-3 noise runs)
against an ns-3 reference on the same flow file.

GeDES CSV: source_ip,dst_ip,flow_size,start_timestamp,completed_timestamp,flow_completion_time
ns-3 CSV (twins-ref.cc): src,dst,flow_size,start_ns,established_ns,completed_ns
Flows are matched by (src host, start time, size). The ns-3 FCT starts when the connection is
established (GeDES assumes established connections).

Metrics: the GeDES paper's "FCT loss" (relative error of the mean FCT), per-flow relative
errors, FCT percentiles and the Kolmogorov-Smirnov distance between FCT distributions.
"""
import argparse
import csv
import numpy as np

IP_BASE = 0xC0A80000


def host_of_ip(ip, half_k):
    ip -= IP_BASE
    return (ip // 48) * half_k + ip % 48


def load_gedes(path, k):
    out = {}
    for r in csv.DictReader(open(path)):
        h = host_of_ip(int(r["source_ip"]), k // 2)
        fct = int(r["flow_completion_time"]) if int(r["completed_timestamp"]) >= 0 else np.nan
        out[(h, int(r["start_timestamp"]), int(r["flow_size"]))] = fct
    return out


def load_ns3(path, by_row=False):
    out = {}
    for i, r in enumerate(csv.DictReader(open(path))):
        done, est = int(r["completed_ns"]), int(r["established_ns"])
        fct = done - est if done >= 0 and est >= 0 else np.nan
        out[i if by_row else (int(r["src"]), int(r["start_ns"]), int(r["flow_size"]))] = fct
    return out


def ks(a, b):
    a, b = np.sort(a), np.sort(b)
    x = np.concatenate([a, b])
    return np.max(np.abs(np.searchsorted(a, x, side="right") / len(a) - np.searchsorted(b, x, side="right") / len(b)))


def compare(name, sim, ref):
    keys = sorted(set(sim) & set(ref))
    a = np.array([sim[k] for k in keys], dtype=float)
    b = np.array([ref[k] for k in keys], dtype=float)
    ok = ~np.isnan(a) & ~np.isnan(b)
    a, b = a[ok], b[ok]
    rel = np.abs(a - b) / b
    q = lambda x, p: np.quantile(x, p) / 1e3
    print(f"{name:34s} n={len(a):5d} mean-FCT err {100 * abs(a.mean() - b.mean()) / b.mean():5.1f}%  "
          f"per-flow err p50 {100 * np.median(rel):5.1f}% p90 {100 * np.quantile(rel, 0.9):5.1f}%  "
          f"FCT p50 {q(a, .5):7.1f}/{q(b, .5):7.1f} p99 {q(a, .99):7.1f}/{q(b, .99):7.1f} us  KS {ks(a, b):.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, required=True)
    ap.add_argument("--ref", required=True, help="ns-3 CSV used as ground truth")
    ap.add_argument("--gedes", nargs="*", default=[], help="GeDES-format CSVs")
    ap.add_argument("--ns3", nargs="*", default=[], help="ns-3-format CSVs (TwiNS, ns-3 noise runs)")
    ap.add_argument("--by_row", action="store_true",
                    help="match ns-3-format CSVs by row (flow-file order), e.g. against start-jittered runs")
    a = ap.parse_args()
    ref = load_ns3(a.ref, a.by_row)
    if a.by_row and a.gedes:
        ap.error("--by_row does not apply to GeDES CSVs")
    for p in a.gedes:
        compare(p.split("/")[-1], load_gedes(p, a.k), ref)
    for p in a.ns3:
        compare(p.split("/")[-1], load_ns3(p, a.by_row), ref)


if __name__ == "__main__":
    main()
