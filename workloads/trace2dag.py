#!/usr/bin/env python3
"""A SimAI run's trace (validation/simai/trace_patch.py, TWINS_TRACE) as a message DAG with
timers (docs/ai/dag.md, "Traced DAGs"), and its fct.txt as a reference per message.

usage: trace2dag.py TRACE FCT DAG_OUT REF_OUT

DAG lines: "flow ID SRC DST SIZE", "timer ID DELAY_NS", and one "on CHILD CAUSE ORD" per flow
(CHILD f<id>) and timer (t<id>): CAUSE is r<flow> (its QpComplete), s<flow> (its SendComplete),
t<timer> (it fired) or m (main, before the run: ORD is ns-3's uid there, else the child's ordinal
among the actions of the cause's callback). REF: "id start_ns fct_ns" per flow, from SimAI's
fct.txt (QpComplete at the sender: sip dip sport dport size start fct base).
"""
import sys

trace, fct, dag_out, ref_out = sys.argv[1:5]
flows, timers, ons, o0 = [], [], [], {}
send_lat = set()
for line in open(trace):
    x = line.split()
    if x[0] == "F":
        i, src, dst, size, sport, sl = int(x[1]), int(x[2]), int(x[3]), int(x[4]), int(x[5]), int(x[6])
        assert i == len(flows)
        flows.append((src, dst, size, sport))
        send_lat.add(sl * 1000)  # (SendFlow's Time(send_lat * 1000) in ns time steps)
        ons.append(("f%d" % i, x[7], int(x[8]), int(x[9])))
    elif x[0] == "T":
        i, d = int(x[1]), float(x[2])
        assert i == len(timers) and d == int(d), line
        timers.append(int(d))
        ons.append(("t%d" % i, x[3], int(x[4]), int(x[5])))
    elif x[0] == "C":
        o0[(x[1], int(x[2]))] = int(x[3])
assert len(send_lat) == 1, send_lat
with open(dag_out, "w") as f:
    f.write("# traced: SEND_LAT_NS=%d\n" % send_lat.pop())
    for i, (src, dst, size, _) in enumerate(flows):
        f.write("flow %d %d %d %d\n" % (i, src, dst, size))
    for i, d in enumerate(timers):
        f.write("timer %d %d\n" % (i, d))
    for child, k, cid, o in ons:
        if k == "m":
            f.write("on %s m %d\n" % (child, o))
        else:
            assert cid >= 0, (child, k)
            f.write("on %s %s%d %d\n" % (child, k, cid, o - o0[(k, cid)]))


def node(ip):
    v = ip - 0x0B000001
    return (v >> 16) * 256 + ((v >> 8) & 0xFF)


of = {(src, dst, sport): i for i, (src, dst, _, sport) in enumerate(flows)}
with open(ref_out, "w") as f:
    for line in open(fct):
        x = line.split()
        k = (node(int(x[0], 16)), node(int(x[1], 16)), int(x[2]))
        f.write("%d %s %s\n" % (of[k], x[5], x[6]))
print("%d flows, %d timers" % (len(flows), len(timers)))
