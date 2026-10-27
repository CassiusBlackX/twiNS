#!/usr/bin/env python3
"""Topology files for TwiNS (--topo=graph --topofile=F) and the ns-3 scenario (--topo=file).

Format:
    hosts H
    switches S
    link A B        # A, B: h<i> (host) or s<j> (switch); one line per point-to-point link
Links are installed in file order, so a node's ports (ns-3 devices) are numbered in the order
its links appear. Every host has exactly one link; all links run at 100 Gbps with 1 us delay.

usage: topo.py fattree K | leafspine LEAVES SPINES HPL | jellyfish SWITCHES NETPORTS HPS SEED
              | torus X Y HPS | dragonfly A G H P    > topo.txt
"""
import random
import sys


def emit(n_hosts, n_switches, links):
    print("hosts %d" % n_hosts)
    print("switches %d" % n_switches)
    for a, b in links:
        print("link %s %s" % (a, b))


def fattree(k):
    # the order of validation/ns3/twins-ref.cc: host links, then aggregation-edge, then core-aggregation
    hk = k // 2
    H, E = k ** 3 // 4, k * hk
    edge = lambda i: "s%d" % i
    agg = lambda i: "s%d" % (E + i)
    core = lambda i: "s%d" % (2 * E + i)
    links = [("h%d" % h, edge(h // hk)) for h in range(H)]
    for pod in range(k):
        for a in range(hk):
            for e in range(hk):
                links.append((agg(pod * hk + a), edge(pod * hk + e)))
    for i in range(hk):
        for j in range(hk):
            for pod in range(k):
                links.append((core(i * hk + j), agg(pod * hk + i)))
    emit(H, 2 * E + hk * hk, links)


def leafspine(leaves, spines, hpl):
    links = [("h%d" % h, "s%d" % (h // hpl)) for h in range(leaves * hpl)]
    for l in range(leaves):
        for s in range(spines):
            links.append(("s%d" % l, "s%d" % (leaves + s)))
    emit(leaves * hpl, leaves + spines, links)


def jellyfish(n, d, hps, seed):
    # Jellyfish (Singla et al., NSDI 2012): join random pairs of switches with free ports that are
    # not yet neighbours; when none is left but a switch still has two free ports, break a random
    # link (x, y) and join both ends to it. hps hosts per switch.
    rng = random.Random(seed)
    free = [d] * n
    adj = [set() for _ in range(n)]
    edges = []
    while True:
        avail = [s for s in range(n) if free[s] > 0]
        pairs = [(a, b) for i, a in enumerate(avail) for b in avail[i + 1:] if b not in adj[a]]
        if pairs:
            a, b = rng.choice(pairs)
            adj[a].add(b)
            adj[b].add(a)
            edges.append((a, b))
            free[a] -= 1
            free[b] -= 1
            continue
        stuck = [s for s in range(n) if free[s] >= 2]
        if not stuck:
            break
        s = rng.choice(stuck)
        cand = [e for e in edges if s not in e and e[0] not in adj[s] and e[1] not in adj[s]]
        if not cand:
            break
        x, y = rng.choice(cand)
        edges.remove((x, y))
        adj[x].discard(y)
        adj[y].discard(x)
        for z in (x, y):
            adj[s].add(z)
            adj[z].add(s)
            edges.append((s, z))
        free[s] -= 2
    links = [("h%d" % h, "s%d" % (h // hps)) for h in range(n * hps)]
    links += [("s%d" % min(a, b), "s%d" % max(a, b)) for a, b in edges]
    emit(n * hps, n, links)


def torus(x, y, hps):
    n = x * y
    links = [("h%d" % h, "s%d" % (h // hps)) for h in range(n * hps)]
    for i in range(x):
        for j in range(y):
            s = i * y + j
            links.append(("s%d" % s, "s%d" % (i * y + (j + 1) % y)))
            links.append(("s%d" % s, "s%d" % (((i + 1) % x) * y + j)))
    emit(n * hps, n, links)


def dragonfly(a, g, h, p):
    # g groups of a routers; routers of a group fully connected; router r of group i has h global
    # links (group pairs joined in a round-robin), p hosts each
    n = a * g
    links = [("h%d" % x, "s%d" % (x // p)) for x in range(n * p)]
    for i in range(g):
        for r in range(a):
            for q in range(r + 1, a):
                links.append(("s%d" % (i * a + r), "s%d" % (i * a + q)))
    port = [0] * n
    for i in range(g):
        for j in range(i + 1, g):
            ri = min(range(i * a, i * a + a), key=lambda r: port[r])
            rj = min(range(j * a, j * a + a), key=lambda r: port[r])
            if port[ri] >= h or port[rj] >= h:
                sys.exit("not enough global links")
            port[ri] += 1
            port[rj] += 1
            links.append(("s%d" % ri, "s%d" % rj))
    emit(n * p, n, links)


if __name__ == "__main__":
    kind, args = sys.argv[1], [int(v) for v in sys.argv[2:]]
    {"fattree": fattree, "leafspine": leafspine, "jellyfish": jellyfish, "torus": torus, "dragonfly": dragonfly}[kind](*args)
