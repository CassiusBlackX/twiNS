#!/usr/bin/env python3
"""nvl_compare.py RECORD ISLAND_OUT...: every completion the islands made (twins-island --isl-out)
against the GPU's (twins-ai --nvl-record's F): time, base, first code, and the whole chain (inline
runs, hashes, root, and the tail runs, followed through each file's tail nodes; DEPTH of them).
"""
import struct
import sys

REC = struct.Struct("<IIq" + "qiIII" + "12I" + "QQ4QII" + "IIQQ")
DEPTH = 4096


def load(path):
    nodes, comps = {}, {}
    with open(path, "rb") as f:
        while True:
            b = f.read(REC.size)
            if len(b) < REC.size:
                break
            v = REC.unpack(b)
            tag, avail = v[0], v[2]
            t, flow, kind, base, code = v[3:8]
            runs = v[8:20]
            tail, hsh = v[20], v[21]
            anc = v[22:26]
            root = v[26]
            ncode, ncnt, nnext, npos = v[28:32]
            if tag == 0:
                nodes[npos] = (ncode, ncnt, nnext)
            elif tag == 1:
                comps[(flow, kind)] = (t, base, code, runs, tail, hsh, anc, root)
    return nodes, comps


def tail_runs(nodes, p):
    out = []
    while p and len(out) < DEPTH:
        if p not in nodes:
            out.append(("cut",))
            break
        c, n, p = nodes[p]
        out.append((c, n))
    return out


rn, rc = load(sys.argv[1])
same = differ = missing = 0
shown = 0
for path in sys.argv[2:]:
    inode, ic = load(path)
    for key, a in ic.items():
        b = rc.get(key)
        if b is None:
            missing += 1
            continue
        ok = a[:4] == b[:4] and a[5:] == b[5:]
        ta, tb = tail_runs(inode, a[4]), tail_runs(rn, b[4])
        # (up to where either file's tail was cut: beyond a recording's depth)
        n = min(len(ta), len(tb))
        for i in range(n):
            if ta[i] == ("cut",) or tb[i] == ("cut",):
                n = i
                break
        ok = ok and ta[:n] == tb[:n] and (len(ta) == len(tb) or n < min(len(ta), len(tb)) or ("cut",) in (ta[-1:] + tb[-1:]))
        if ok:
            same += 1
        else:
            differ += 1
            if shown < 5:
                shown += 1
                print("differs: flow %d kind %d: island t %d base %d code %d runs %s | GPU t %d base %d code %d runs %s" %
                      (key[0], key[1], a[0], a[1], a[2], a[3][:6], b[0], b[1], b[2], b[3][:6]))
print("%d completions identical, %d differ, %d not in the recording (of %d recorded)" % (same, differ, missing, len(rc)))
