#!/usr/bin/env python3
"""Multi-seed marking-threshold sweep (TODOS R-3, paper Figure 1).

usage: ksweep_stats.py DIR [TCP] [QUEUE]
DIR holds ws30_s<seed>_<Tcp>_q<queue>_K<K>.csv (ns-3 format). For short flows (< 100 KB):
per seed, the p99 FCT for each K; the share of short flows that waited out an RTO (FCT > 1 ms);
over seeds: mean and spread of p99 per K, the best K per seed, Kendall's tau between seeds' K
rankings, and the regret of choosing K from one seed (p99 of that K on the other seeds over their
own best p99).
"""
import csv, glob, itertools, os, re, statistics, sys
from collections import defaultdict

SHORT = 100000
RTO_PS = 1000 * 1000  # FCT in ns in the files: 1 ms = 1e6 ns


def short_fcts(p):
    out = []
    with open(p) as f:
        r = csv.reader(f)
        next(r)
        for row in r:
            if int(row[2]) < SHORT and int(row[5]) >= 0:
                out.append(int(row[5]) - int(row[3]))
    return out


def p99(xs):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(0.99 * (len(xs) - 1) + 0.5))]


def kendall(a, b):
    n = c = d = 0
    for i, j in itertools.combinations(range(len(a)), 2):
        s = (a[i] - a[j]) * (b[i] - b[j])
        if s > 0:
            c += 1
        elif s < 0:
            d += 1
    return (c - d) / max(1, c + d)


def main():
    d = sys.argv[1]
    tcp = sys.argv[2] if len(sys.argv) > 2 else 'TcpDctcp'
    qsel = sys.argv[3] if len(sys.argv) > 3 else None
    data = defaultdict(dict)  # (q) -> seed -> {K: (p99, rto share, n)}
    for p in glob.glob(os.path.join(d, 'ws30_s*_%s_q*_K*.csv' % tcp)):
        m = re.match(r'ws30_s(\d+)_\w+?_q(\d+)_K(\d+)\.csv', os.path.basename(p))
        seed, q, K = int(m.group(1)), int(m.group(2)), int(m.group(3))
        f = short_fcts(p)
        data[q].setdefault(seed, {})[K] = (p99(f), sum(1 for x in f if x > RTO_PS) / len(f), len(f))
    for q in sorted(data):
        if qsel and str(q) != qsel:
            continue
        seeds = sorted(data[q])
        Ks = sorted(set.intersection(*[set(data[q][s]) for s in seeds]))
        print('== %s, queue %d packets: %d seeds, K in %s, short flows per seed %d..%d' % (
            tcp, q, len(seeds), Ks, min(data[q][s][Ks[0]][2] for s in seeds), max(data[q][s][Ks[0]][2] for s in seeds)))
        for K in Ks:
            v = [data[q][s][K][0] / 1000 for s in seeds]
            r = [100 * data[q][s][K][1] for s in seeds]
            print('  K=%3d  p99 short FCT (us): median %7.0f  min %7.0f  max %7.0f | RTO share %%: median %.2f max %.2f' % (
                K, statistics.median(v), min(v), max(v), statistics.median(r), max(r)))
        best = {s: min(Ks, key=lambda K: data[q][s][K][0]) for s in seeds}
        hist = defaultdict(int)
        for s in seeds:
            hist[best[s]] += 1
        print('  best K per seed: %s' % dict(sorted(hist.items())))
        taus = [kendall([data[q][a][K][0] for K in Ks], [data[q][b][K][0] for K in Ks]) for a, b in itertools.combinations(seeds, 2)]
        print('  Kendall tau between seeds: median %.2f, min %.2f' % (statistics.median(taus), min(taus)))
        reg = []
        for a in seeds:
            for b in seeds:
                if a != b:
                    reg.append(data[q][b][best[a]][0] / data[q][b][best[b]][0] - 1)
        print('  regret of K chosen on one seed, on another: median %.0f%%, p90 %.0f%%, max %.0f%%' % (
            100 * statistics.median(reg), 100 * sorted(reg)[int(0.9 * (len(reg) - 1))], 100 * max(reg)))
        # the average over seeds: what a 20-seed study would pick
        avg = {K: statistics.mean(data[q][s][K][0] for s in seeds) for K in Ks}
        print('  K by mean p99 over seeds: best %d; mean p99 (us) %s' % (min(avg, key=avg.get), {K: round(v / 1000) for K, v in avg.items()}))


if __name__ == '__main__':
    main()
