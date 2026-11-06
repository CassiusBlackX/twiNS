#!/usr/bin/env python3
"""Generate ns-3-ub case directories for TwiNS-UB validation.

Usage: gen_cases.py OUTDIR NAME KIND [key=val ...]
  KIND clos : hosts= leaves= spines= bw= delay= alloc= fwd=
  traffic   : traffic=alltoall|allreduce|... size=... (ub_tools.py traffic), or
              traffic=rand flows= size= seed= start=
  attr      : attr=FILE  network_attribute.txt to copy (default: base.attr next to this file)
              set:KEY=VAL lines appended/replaced in network_attribute.txt
Needs ns-3-ub-tools on sys.path (UB_TOOLS env, default ~/Work/SpecDES/ns-3-ub/scratch/ns-3-ub-tools).
"""
import os, sys, random, subprocess, shutil

TOOLS = os.environ.get("UB_TOOLS", os.path.expanduser("~/Work/SpecDES/ns-3-ub/scratch/ns-3-ub-tools"))
sys.path.insert(0, TOOLS)
import networkx as nx
import net_sim_builder as netsim


def shortest(G, s, t):
    try:
        return nx.all_shortest_paths(G, s, t)
    except nx.NetworkXNoPath:
        return []


def clos(out, hosts=32, leaves=4, spines=4, bw="400Gbps", delay="20ns", alloc="1ns", fwd="0ns",
         hbw=None, hdelay=None, **_):
    hosts, leaves, spines = int(hosts), int(leaves), int(spines)
    g = netsim.NetworkSimulationGraph()
    g.output_dir = out + "/"
    for h in range(hosts):
        g.add_netisim_host(h, allocation_delay=alloc, forward_delay=fwd)
    L = [hosts + i for i in range(leaves)]
    S = [hosts + leaves + i for i in range(spines)]
    for n in L + S:
        g.add_netisim_node(n, allocation_delay=alloc, forward_delay=fwd)
    per = hosts // leaves
    for h in range(hosts):
        g.add_netisim_edge(h, L[h // per], bandwidth=hbw or bw, delay=hdelay or delay)
    for l in L:
        for s in S:
            g.add_netisim_edge(l, s, bandwidth=bw, delay=delay)
    g.build_graph_config()
    g.gen_route_table(path_finding_algo=shortest)
    g.write_config(include_transport=False)
    return hosts


def rand_traffic(out, hosts, flows=64, size="65536", seed=1, start="10ns", op="URMA_WRITE", pri=7, **_):
    rnd = random.Random(int(seed))
    with open(os.path.join(out, "traffic.csv"), "w") as f:
        f.write("taskId,sourceNode,destNode,dataSize(Byte),opType,priority,delay,phaseId,dependOnPhases\n")
        for i in range(int(flows)):
            s = rnd.randrange(hosts)
            d = rnd.randrange(hosts - 1)
            d += d >= s
            sz = int(size) if not str(size).startswith("r") else rnd.randrange(1, int(size[1:]))
            f.write(f"{i},{s},{d},{sz},{op},{pri},{start},0,\n")


def main():
    out, name, kind = sys.argv[1], sys.argv[2], sys.argv[3]
    kv = dict(a.split("=", 1) for a in sys.argv[4:] if not a.startswith("set:"))
    sets = [a[4:] for a in sys.argv[4:] if a.startswith("set:")]
    d = os.path.join(out, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    hosts = {"clos": clos}[kind](d, **kv)
    tr = kv.get("traffic", "rand")
    if tr == "rand":
        rand_traffic(d, hosts, **kv)
    else:
        args = [sys.executable, os.path.join(TOOLS, "ub_tools.py"), "traffic", tr, "--host-count", str(hosts),
                "--data-size", kv.get("size", "64KB"), "--algorithm", kv.get("algo", "pairwise"),
                "--output-dir", d] + kv.get("targs", "").split()
        subprocess.run(args, check=True, stdout=subprocess.DEVNULL)
    attr = kv.get("attr", os.path.join(os.path.dirname(os.path.abspath(__file__)), "base.attr"))
    lines = open(attr).read().splitlines()
    for s in sets:
        key, val = s.split("=", 1)
        lines = [l for l in lines if not (len(l.split()) >= 2 and l.split()[1] == key)]
        pref = "global" if key.startswith("UB_") else "default"
        lines.append(f'{pref} {key} "{val}"')
    open(os.path.join(d, "network_attribute.txt"), "w").write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
