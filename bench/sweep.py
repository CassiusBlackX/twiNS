#!/usr/bin/env python3
"""Parameter sweep with TwiNS: ECN/RED threshold K x queue limit x TCP x workload, run as
batches of worlds (--worlds). Writes one ns-3-format CSV per configuration and prints the wall
time (the simulator's own timing of the event loop, summed over batches, and the total process
time including set-up).

usage: bench/sweep.py --out DIR [--batch N] [--k 8] [--workloads k8_a2a30,k8_a2a50w]
                      [--queues 100,200,400] [--Ks 5,10,20,30,40,60,80,120]
"""
import argparse
import os
import re
import subprocess
import time

TCP = {"dctcp": "TcpDctcp", "cubic": "TcpCubic", "newreno": "TcpNewReno"}


def configs(workloads, queues, Ks):
    for w in workloads:
        for t in TCP:
            for q in queues:
                for K in Ks:
                    if K < q:
                        yield w, t, q, K


def run_long(k, lines, wf):
    r = subprocess.run(["./build/twins128", f"--k={k}", f"--worlds={wf}"], capture_output=True, text=True)
    if r.returncode == 0 or len(lines) == 1:
        if r.returncode:
            raise SystemExit(r.stderr)
        return r
    half = len(lines) // 2
    out = []
    for i, part in enumerate((lines[:half], lines[half:])):
        pf = f"{wf}.{i}"
        open(pf, "w").write("\n".join(part) + "\n")
        out.append(run_long(k, part, pf))
    # the simulation times of the parts add up; keep the last one's report
    d = sum(float(re.search(r"Simulation Duration: ([0-9.]+)s", x.stdout).group(1)) for x in out)
    out[-1].stdout = re.sub(r"Simulation Duration: [0-9.]+s", f"Simulation Duration: {d:.6f}s", out[-1].stdout)
    out[-1].stderr = "".join(x.stderr for x in out)
    return out[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--batch", type=int, default=46)
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--workloads", default="k8_a2a30,k8_a2a50w")
    ap.add_argument("--queues", default="100,200,400")
    ap.add_argument("--Ks", default="5,10,20,30,40,60,80,120")
    ap.add_argument("--hashes", default="",
                    help="an ns-3 output of the same flows to take the ECMP hashes from (default: TwiNS computes ns-3's)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    cfg = list(configs(a.workloads.split(","), [int(x) for x in a.queues.split(",")], [int(x) for x in a.Ks.split(",")]))
    sim = total = 0.0
    for b in range(0, len(cfg), a.batch):
        wf = os.path.join(a.out, f"worlds_{b // a.batch}.txt")
        with open(wf, "w") as f:
            for w, t, q, K in cfg[b:b + a.batch]:
                ecmp = a.hashes.format(w=w)
                ecmp = f",ecmp_from={ecmp}" if os.path.exists(ecmp) else ""
                f.write(f"flows=data/flows/{w}.csv,tcp={t},queue={q},ecnK={K}{ecmp},"
                        f"out={a.out}/{w}_{TCP[t]}_q{q}_K{K}.csv\n")
        t0 = time.time()
        r = subprocess.run(["./build/twins", f"--k={a.k}", f"--worlds={wf}"], capture_output=True, text=True, check=True)
        if "undecided" in r.stderr:
            # ties the recorded histories could not decide: those worlds again, with longer
            # histories (twice the frame memory: in halves if they do not fit)
            m = re.search(r"undecided-ties by world:([ 0-9]*)", r.stdout)
            lines = open(wf).read().splitlines()
            redo = [lines[int(w)] for w in m.group(1).split()] if m else lines
            print(f"batch {b // a.batch}: undecided ties in {len(redo)} worlds, rerun with twins128", flush=True)
            rf = wf + ".long"
            open(rf, "w").write("\n".join(redo) + "\n")
            rl = run_long(a.k, redo, rf)
            sim += float(re.search(r"Simulation Duration: ([0-9.]+)s", rl.stdout).group(1))
            r.stderr = rl.stderr
        total += time.time() - t0
        bad = [l for l in r.stderr.splitlines() if "warning" in l and "undecided" not in l]
        if bad:
            raise SystemExit(f"batch {b // a.batch}: {bad[0]}")
        if "undecided" in r.stderr:
            print(f"batch {b // a.batch}: ties still undecided with twins128 (results may differ from ns-3)", flush=True)
        sim += float(re.search(r"Simulation Duration: ([0-9.]+)s", r.stdout).group(1))
        last = [l for l in r.stdout.splitlines() if l.startswith("flows ")][-1]
        print(f"batch {b // a.batch}: {min(a.batch, len(cfg) - b)} worlds, {last[:90]}", flush=True)
    print(f"{len(cfg)} configurations in batches of {a.batch}: simulation {sim:.2f} s, total {total:.2f} s")


if __name__ == "__main__":
    main()
