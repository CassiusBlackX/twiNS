#!/usr/bin/env python3
"""Topology files for TwiNS (--topo=graph --topofile=F) and the ns-3 scenario (--topo=file).

Format:
    hosts H
    switches S
    link A B        # A, B: h<i> (host) or s<j> (switch); one line per point-to-point link
Links are installed in file order, so a node's ports (ns-3 devices) are numbered in the order
its links appear. Every host has exactly one link; all links run at 100 Gbps with 1 us delay.

usage: topo.py fattree K | leafspine LEAVES SPINES HPL | jellyfish SWITCHES NETPORTS HPS SEED
              | jellyfish2 SWITCHES NETPORTS HPS SEED | torus X Y HPS | torus3 X Y Z HPS
              | dragonfly A G H P | hyperx HPS D1 D2 [D3 ...] | xpander D LIFT HPS SEED    > topo.txt
(jellyfish2: the same random regular graph family, built in near-linear time for large sizes)
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


def jellyfish2(n, d, hps, seed):
    # Jellyfish for thousands of switches: random stub matching (pick two random switches with free
    # ports that are not yet neighbours, a bounded number of tries), then the NSDI 2012 repair (break
    # a random link and join both ends to a switch left with two or more free ports)
    rng = random.Random(seed)
    free = [d] * n
    adj = [set() for _ in range(n)]
    edges = set()
    avail = list(range(n))
    misses = 0
    while len(avail) > 1 and misses < 50 * n:
        a, b = rng.sample(avail, 2)
        if b in adj[a]:
            misses += 1
            continue
        adj[a].add(b)
        adj[b].add(a)
        edges.add((min(a, b), max(a, b)))
        for z in (a, b):
            free[z] -= 1
        avail = [z for z in avail if free[z] > 0] if free[a] == 0 or free[b] == 0 else avail
    for s in range(n):
        while free[s] >= 2:
            el = list(edges)
            for _ in range(1000):
                x, y = rng.choice(el)
                if s not in (x, y) and x not in adj[s] and y not in adj[s]:
                    break
            else:
                break
            edges.discard((x, y))
            adj[x].discard(y)
            adj[y].discard(x)
            for z in (x, y):
                adj[s].add(z)
                adj[z].add(s)
                edges.add((min(s, z), max(s, z)))
            free[s] -= 2
    links = [("h%d" % h, "s%d" % (h // hps)) for h in range(n * hps)]
    links += [("s%d" % a, "s%d" % b) for a, b in sorted(edges)]
    emit(n * hps, n, links)


def torus3(x, y, z, hps):
    # a 3-D torus (TPU and HPC machines): every switch joined to its 6 neighbours, wrapping around
    n = x * y * z
    sid = lambda i, j, l: (i * y + j) * z + l
    links = [("h%d" % h, "s%d" % (h // hps)) for h in range(n * hps)]
    for i in range(x):
        for j in range(y):
            for l in range(z):
                s = sid(i, j, l)
                for t in (sid((i + 1) % x, j, l), sid(i, (j + 1) % y, l), sid(i, j, (l + 1) % z)):
                    if t != s:
                        links.append(("s%d" % s, "s%d" % t))
    emit(n * hps, n, links)


def hyperx(hps, *dims):
    # HyperX (Ahn et al., SC 2009): switches on a grid of the given dimensions, each joined to every
    # switch that differs from it in exactly one coordinate
    n = 1
    for d in dims:
        n *= d
    links = [("h%d" % h, "s%d" % (h // hps)) for h in range(n * hps)]
    stride = [1] * len(dims)
    for i in range(len(dims) - 2, -1, -1):
        stride[i] = stride[i + 1] * dims[i + 1]
    for s in range(n):
        for i, d in enumerate(dims):
            c = s // stride[i] % d
            for q in range(c + 1, d):
                links.append(("s%d" % s, "s%d" % (s + (q - c) * stride[i])))
    emit(n * hps, n, links)


def xpander(d, lift, hps, seed):
    # Xpander (Valadarsky et al., CoNEXT 2016): a random lift of the complete graph K_{d+1}; each
    # of its edges becomes a random perfect matching between the lift copies of its two ends, so
    # every switch has d network links
    rng = random.Random(seed)
    n = (d + 1) * lift
    links = [("h%d" % h, "s%d" % (h // hps)) for h in range(n * hps)]
    for u in range(d + 1):
        for v in range(u + 1, d + 1):
            perm = list(range(lift))
            rng.shuffle(perm)
            for c in range(lift):
                links.append(("s%d" % (u * lift + c), "s%d" % (v * lift + perm[c])))
    emit(n * hps, n, links)


if __name__ == "__main__":
    kind, args = sys.argv[1], [int(v) for v in sys.argv[2:]]
    {"fattree": fattree, "leafspine": leafspine, "jellyfish": jellyfish, "jellyfish2": jellyfish2, "torus": torus,
     "torus3": torus3, "dragonfly": dragonfly, "hyperx": hyperx, "xpander": xpander}[kind](*args)
