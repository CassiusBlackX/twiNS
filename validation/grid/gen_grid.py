#!/usr/bin/env python3
"""A randomized exactness grid (TODOS P0-7): N scenarios drawn at random over topology family and
size, traffic pattern and flow sizes, load, TCP, queue and marking threshold, per-flow ECMP or
spraying, persistent connections and FIN, and link failures. Writes

    data/grid/topo/<t>.txt, data/grid/<name>.csv       topologies and flows
    validation/grid/jobs_ns3.txt                        ns-3 jobs (validation/ns3/run_jobs.sh; paths
                                                        relative to its work directory, under grid/)
    validation/grid/jobs_twins.txt                      name, then TwiNS's arguments

usage: validation/grid/gen_grid.py [N] [SEED]      (from the repository root)
"""
import os, random, subprocess, sys

N = int(sys.argv[1]) if len(sys.argv) > 1 else 200
SEED = int(sys.argv[2]) if len(sys.argv) > 2 else 2027
rng = random.Random(SEED)
D, T = "data/grid", "data/grid/topo"
os.makedirs(T, exist_ok=True)

TCPS = ["dctcp", "newreno", "cubic"]
NS3_TCP = {"dctcp": "TcpDctcp", "newreno": "TcpNewReno", "cubic": "TcpCubic"}


def topo():
    """(name, topo.py arguments or None for a native FatTree k, hosts, failable)"""
    kind = rng.choice(["fattree", "fattree", "leafspine", "jellyfish", "torus", "torus3", "dragonfly", "hyperx", "xpander"])
    if kind == "fattree":
        k = rng.choice([4, 8, 8])
        return ("ft%d" % k, ["fattree", k], k ** 3 // 4, True)
    if kind == "leafspine":
        while True:
            L, S, H = rng.choice([4, 8, 16]), rng.choice([2, 4, 8]), rng.choice([4, 8, 12, 16])
            if S + H <= 32 and L <= 32:
                return ("ls%dx%dx%d" % (L, S, H), ["leafspine", L, S, H], L * H, False)
    if kind == "jellyfish":
        S, P, H, s = rng.choice([16, 24, 32, 48]), rng.choice([4, 6, 8]), rng.choice([2, 3, 4]), rng.randrange(100)
        return ("jf%d_%d_%d_%d" % (S, P, H, s), ["jellyfish", S, P, H, s], S * H, True)
    if kind == "torus":
        X, Y, H = rng.choice([4, 6, 8]), rng.choice([4, 6, 8]), rng.choice([2, 4])
        return ("tor%dx%dx%d" % (X, Y, H), ["torus", X, Y, H], X * Y * H, True)
    if kind == "torus3":
        return ("t3d4x%d" % 2, ["torus3", 4, 4, 4, 2], 128, True)
    if kind == "dragonfly":
        a, h, p = rng.choice([(4, 2, 2), (4, 2, 4), (6, 3, 3)])
        g = a * h + 1
        return ("df%d_%d_%d" % (a, h, p), ["dragonfly", a, g, h, p], a * g * p, False)
    if kind == "hyperx":
        H, dims = rng.choice([(4, [4, 4]), (2, [8, 4]), (4, [4, 4, 2])])
        return ("hx%d_%s" % (H, "x".join(map(str, dims))), ["hyperx", H] + dims, H * eval("*".join(map(str, dims))), False)
    d, lift, H, s = rng.choice([4, 5, 6]), rng.choice([4, 6, 8]), rng.choice([2, 3]), rng.randrange(100)
    return ("xp%d_%d_%d_%d" % (d, lift, H, s), ["xpander", d, lift, H, s], (d + 1) * lift * H, False)


def flows(hosts, out, reuse):
    pat = rng.choice(["alltoall", "alltoall", "alltoall", "incast", "pair"])
    g = ["python3", "workloads/gen.py", "--hosts", str(hosts), "--seed", str(rng.randrange(1 << 20)), "--out", out]
    if pat == "alltoall":
        size = rng.choice(["websearch", "websearch", "hadoop", "mining", "exp", "uniform", "fixed"])
        load = rng.choice([0.2, 0.3, 0.4, 0.5, 0.6])
        dur = rng.choice([500000, 1000000])
        g += ["--pattern", "alltoall", "--load", str(load), "--duration_ns", str(dur)]
        if size in ("hadoop", "mining"):
            g += ["--size", "cdf", "--cdf", "workloads/cdf/%s.txt" % ("fb_hadoop" if size == "hadoop" else "datamining"),
                  "--align", "byte"]
        elif size == "websearch":
            g += ["--size", "websearch"]
        else:
            g += ["--size", size, "--align", rng.choice(["mss", "byte"])]
        desc = "a2a %s %.0f%%" % (size, 100 * load)
    elif pat == "incast":
        fan = rng.choice([4, 8, 16, 32])
        fan = min(fan, hosts - 1)
        g += ["--pattern", "incast", "--load", "0", "--fanin", str(fan), "--incast_bytes", str(rng.choice([46720, 93440, 186880])),
              "--incast_period_ns", str(rng.choice([100000, 200000])), "--duration_ns", "1000000"]
        desc = "incast %d-to-1" % fan
    else:
        g += ["--pattern", "pair", "--size", rng.choice(["fixed", "uniform"]), "--start", rng.choice(["zero", "uniform"]),
              "--spread_ns", "20000"]
        desc = "pairs"
    if reuse and pat == "alltoall":
        g += ["--reuse", "1"]
        desc += ", persistent"
    subprocess.run(g, check=True, stdout=subprocess.DEVNULL)
    return desc


def failures(tfile):
    links = [l.split()[1:] for l in open(tfile) if l.startswith("link s")]
    picks = rng.sample(links, rng.choice([1, 2]))
    spec, t = [], rng.choice([100000, 200000, 300000])
    for a, b in picks:
        spec.append("%d:%s:%s" % (t, a, b))
        if rng.random() < 0.5:
            spec.append("%d:%s:%s:up" % (t + rng.choice([200000, 400000]), a, b))
        t += rng.choice([100000, 200000])
    return ",".join(sorted(spec, key=lambda s: int(s.split(":")[0])))


ns3, tw, desc = [], [], []
for i in range(N):
    tname, targs, hosts, failable = topo()
    tfile = "%s/%s.txt" % (T, tname)
    if not os.path.exists(tfile):
        with open(tfile, "w") as f:
            subprocess.run(["python3", "workloads/topo.py"] + [str(a) for a in targs], check=True, stdout=f)
    hosts = int(open(tfile).readline().split()[1])
    name = "g%03d" % i
    reuse = rng.random() < 0.25
    fdesc = flows(hosts, "%s/%s.csv" % (D, name), reuse)
    tcp = rng.choice(TCPS)
    queue = rng.choice([50, 100, 200, 400])
    K = rng.choice([k for k in [5, 10, 20, 30, 40, 60, 80, 120] if k < queue])
    spray = rng.random() < 0.3
    fin = rng.random() < 0.25
    fail = failures(tfile) if failable and rng.random() < 0.25 else ""
    common_tw = ["--topo=graph", "--topofile=%s" % tfile]
    common_ns = ["--topo=file", "--topofile=grid/topo/%s.txt" % tname]
    extra_tw, extra_ns = ["--queue=%d" % queue, "--ecnK=%d" % K], ["--queue=%d" % queue, "--ecnK=%d" % K]
    if spray:
        extra_tw.append("--spray=1")
    if fin:
        extra_tw.append("--fin=1")
        extra_ns.append("--fin=1")
    if fail:
        extra_tw.append("--fail=%s" % fail)
        extra_ns.append("--fail=%s" % fail)
    ns3.append(" ".join([name] + common_ns + extra_ns + ["--flows=grid/%s.csv" % name, "--tcp=ns3::%s" % NS3_TCP[tcp],
                                                          "--stop=0.3", "--hostq=red", "--flowEcmp=%d" % (0 if spray else 1)]))
    tw.append(" ".join([name] + common_tw + ["--flows=%s/%s.csv" % (D, name), "--tcp=%s" % tcp] + extra_tw))
    desc.append("%s %s: %d hosts, %s, %s, queue %d K %d%s%s%s" % (name, tname, hosts, fdesc, tcp, queue, K, ", spray" if spray else "",
                                                               ", FIN" if fin else "", ", failures " + fail if fail else ""))
with open("validation/grid/jobs_ns3.txt", "w") as f:
    f.write("# randomized grid (gen_grid.py %d %d): run from the ns-3 work directory with grid/ copied in\n" % (N, SEED))
    f.write("\n".join(ns3) + "\n")
with open("validation/grid/jobs_twins.txt", "w") as f:
    f.write("# randomized grid (gen_grid.py %d %d)\n" % (N, SEED))
    f.write("\n".join(tw) + "\n")
with open("validation/grid/scenarios.txt", "w") as f:
    f.write("\n".join(desc) + "\n")
print("%d scenarios" % N)
