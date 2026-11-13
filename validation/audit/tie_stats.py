#!/usr/bin/env python3
"""Tie-order sensitivity of sequential ns-3 (TODOS R-3): the same binary, workload and seeds, with
only the order of simultaneous events changed (NS3_TIE_SEED, validation/audit/ns3_evlog_patch.py).

usage: tie_stats.py DIR [REFDIR]
DIR holds NAME_s<seed>.csv (twins-ref output: src,dst,size,start_ns,est_ns,done_ns); seed 0 is
ns-3's own order. With REFDIR, seed 0 is also checked against REFDIR/NAME.csv (the stored
reference). For each NAME and seed > 0: share of flows whose completion time differs, relative
change of mean, p50 and p99 FCT, largest relative per-flow change; then the spread over seeds.
"""
import csv, glob, os, re, statistics, sys
from collections import defaultdict


def fcts(path):
    with open(path) as f:
        r = csv.reader(f)
        next(r)
        out = []
        for row in r:
            start, done = int(row[3]), int(row[5])
            out.append(done - start if done >= 0 else None)
    return out


def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return float('nan')
    k = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return xs[k]


def main():
    d = sys.argv[1]
    ref = sys.argv[2] if len(sys.argv) > 2 else None
    runs = defaultdict(dict)
    for p in glob.glob(os.path.join(d, '*_s*.csv')):
        m = re.match(r'(.*)_s(\d+)\.csv$', os.path.basename(p))
        runs[m.group(1)][int(m.group(2))] = p
    print('%-28s %5s %6s | %-17s %-17s %-17s %-17s %s' % ('run', 'flows', 'seeds', 'flows changed', 'mean FCT', 'p50 FCT', 'p99 FCT',
                                                         'max per-flow'))
    for name in sorted(runs):
        s = runs[name]
        if 0 not in s:
            continue
        base = fcts(s[0])
        if ref:
            rp = os.path.join(ref, name + '.csv')
            if os.path.exists(rp):
                same = fcts(rp) == base
                if not same:
                    print('  ! %s: seed 0 differs from the stored reference' % name)
        ok = [x for x in base if x is not None]
        bm, b50, b99 = statistics.mean(ok), pct(ok, .5), pct(ok, .99)
        ch, dm, d50, d99, mx = [], [], [], [], []
        for seed, p in sorted(s.items()):
            if seed == 0:
                continue
            x = fcts(p)
            if len(x) != len(base):
                continue
            n = sum(1 for a, b in zip(base, x) if a != b)
            ch.append(n / len(base))
            xo = [v for v in x if v is not None]
            dm.append(statistics.mean(xo) / bm - 1)
            d50.append(pct(xo, .5) / b50 - 1)
            d99.append(pct(xo, .99) / b99 - 1)
            mx.append(max(abs(b - a) / a for a, b in zip(base, x) if a and b is not None))
        if not ch:
            continue
        rng = lambda v, f: '%s..%s' % (f % min(v), f % max(v))
        print('%-28s %5d %6d | %-17s %-17s %-17s %-17s %s' % (name, len(base), len(ch), rng([100 * c for c in ch], '%.0f%%'),
                                                            rng([100 * c for c in dm], '%+.2f%%'), rng([100 * c for c in d50], '%+.2f%%'),
                                                            rng([100 * c for c in d99], '%+.1f%%'), '%.0f%%' % (100 * max(mx))))


if __name__ == '__main__':
    main()
