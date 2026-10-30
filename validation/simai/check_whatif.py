#!/usr/bin/env python3
"""A run of twins-rdma on configuration B against SimAI on B, per message, when the DAG was
recorded elsewhere. Flow ids differ between recordings, and SimCCL's own ids too (MoE): a message
is (src, dst, size, its place among these in the order of the recording).

usage: check_whatif.py NAME B.dag B.ref DAG TWINS_OUT [--lines]

Prints NAME, messages, identical (start and fct), makespan of B and of the run (ns) with their
error, the absolute error of completion times as median, p99 and max, and how many messages have
the same cause in both DAGs (the flow's kind and message, through timers, with the same delay).
--lines: the DAGs' lines that differ (twins-ai --dag-out against a recording: same ids, ordinals;
main's ordinals are ns-3's uids, of which only the order counts).
"""
import sys


def read_dag(path):
    """keys by flow id, and each flow's cause: (kind, key of the flow that fired, delay in ns)"""
    keys, occ, delay, on = [], {}, {}, {}
    for line in open(path):
        x = line.split()
        if x[0] == "flow":
            k = tuple(x[2:5])
            occ[k] = occ.get(k, -1) + 1
            keys.append(k + (occ[k],))
        elif x[0] == "timer":
            delay[x[1]] = int(x[2])
        elif x[0] == "on":
            on[x[1]] = x[2]
    cause = {}
    for i, k in enumerate(keys):
        c, d = on["f%d" % i], 0
        while c[0] == "t":
            d += delay[c[1:]]
            c = on[c]
        cause[k] = (c[0], keys[int(c[1:])] if c[0] != "m" else None, d)
    return keys, cause


def times(path, keys):
    return {keys[int(x[0])]: (int(x[1]), int(x[2])) for x in map(str.split, open(path))}


def lines(path):
    """a DAG's lines, order aside; main's actions in the order of their uids"""
    m, r = [], set()
    for line in open(path):
        x = line.split()
        if x[0] == "on" and x[2] == "m":
            m.append(x[1])
            if len(x) > 3:
                m[-1] = (int(x[3]), x[1])
        elif x[0] != "#":
            r.add(line.strip())
    return r, m


name, bdag, bref, dag, out = sys.argv[1:6]
kb, cb = read_dag(bdag)
kd, cd = read_dag(dag)
ref, run = times(bref, kb), times(out, kd)
if set(ref) != set(run):
    print("%s: message sets differ: %d only in SimAI, %d only in the run" % (name, len(set(ref) - set(run)), len(set(run) - set(ref))))
same = sum(1 for k in ref if run.get(k) == ref[k])
err = sorted(abs(sum(run[k]) - sum(ref[k])) for k in ref if k in run)
mr = max(map(sum, ref.values()))
mt = max(map(sum, run.values()))
sc = sum(1 for k in cb if cd.get(k) == cb[k])
print(
    "%-10s %7d msgs %7d same (%6.2f%%)  end %10d vs %10d (%+7.3f%%)  |err| med %8d p99 %9d max %9d ns  same cause %6.2f%%"
    % (name, len(ref), same, 100.0 * same / len(ref), mr, mt, 100.0 * (mt - mr) / mr, err[len(err) // 2], err[len(err) * 99 // 100], err[-1], 100.0 * sc / len(cb))
)
if "--lines" in sys.argv:
    (ra, ma), (rb, mb) = lines(bdag), lines(dag)
    ma = [x[1] if isinstance(x, tuple) else x for x in sorted(ma)]
    print("%-10s DAG lines: %d only in the recording, %d only in this run, of %d; main's actions %s" % (name, len(ra - rb), len(rb - ra), len(ra), "same" if ma == mb else "DIFFER"))
