#!/usr/bin/env python3
"""Convert a TwiNS topology ("hosts H", "switches S", "link A B") to SimAI's format
(gen_Topo_Template.py): "nodes gpus_per_server nvswitches switches links gpu_type", the switch ids,
then "src dst rate delay error_rate" per link, in the same order. Hosts keep ids 0..H-1 and
switches H..H+S-1, so flow files carry over unchanged.

usage: simai_topo.py TOPO RATE DELAY [SWRATE SWDELAY] > OUT   (e.g. fattree8.txt 100Gbps 1000ns)
  RATE, DELAY: host links (and all links); SWRATE, SWDELAY: links between switches
"""
import sys


def main():
    path, rate, delay = sys.argv[1:4]
    swrate, swdelay = (sys.argv[4], sys.argv[5]) if len(sys.argv) > 5 else (rate, delay)
    H = S = 0
    links = []
    with open(path) as f:
        toks = f.read().split()
    i = 0
    while i < len(toks):
        t = toks[i]
        if t == "hosts":
            H = int(toks[i + 1])
            i += 2
        elif t == "switches":
            S = int(toks[i + 1])
            i += 2
        elif t == "link":
            links.append((toks[i + 1], toks[i + 2]))
            i += 3
        else:
            i += 1

    def nid(x):
        return (0 if x[0] == "h" else H) + int(x[1:])

    print(H + S, 1, 0, S, len(links), "H100")
    print(" ".join(str(H + j) for j in range(S)))
    for a, b in links:
        sw = a[0] != "h" and b[0] != "h"
        print(nid(a), nid(b), swrate if sw else rate, swdelay if sw else delay, 0)


if __name__ == "__main__":
    main()
