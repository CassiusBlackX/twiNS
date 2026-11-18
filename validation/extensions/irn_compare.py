"""Run a pinned ConWeave ns-3 executable and compare every IRN flow and wire event."""
import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import time
from collections import Counter
from itertools import zip_longest
from pathlib import Path


def execute(command, cwd, env, log):
    start = time.perf_counter_ns()
    with log.open("w") as f:
        subprocess.run(command, cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT, check=True)
    return time.perf_counter_ns() - start


def wire(path):
    rows = [line.split() for line in path.read_text().splitlines()]
    return sorted(rows, key=lambda row: tuple(map(int, row[:3])))


def flow_ids(inputs):
    with inputs.open() as f:
        flows = list(enumerate(csv.DictReader(f)))
    send, recv = Counter(), Counter()
    ids = {}
    for i, r in sorted(flows, key=lambda pair: int(pair[1]["start_ns"])):
        src, dst, size, start = (int(r[k]) for k in ("src", "dst", "flow_size", "start_ns"))
        ids[src, dst, 10000 + send[src], 100 + recv[dst], size, start] = i
        send[src] += 1
        recv[dst] += 1
    return ids


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("twins", "reference_tree", "fixtures"):
        p.add_argument(key, type=Path)
    p.add_argument("--case", action="append", help="select a fixture (repeatable)")
    p.add_argument("--image", help="local image providing the reference executable's libraries")
    a = p.parse_args()
    twins, ref, fixtures = (x.resolve() for x in (a.twins, a.reference_tree, a.fixtures))
    failures = []
    for d in sorted(f for f in fixtures.iterdir() if f.is_dir()):
        if a.case and d.name not in a.case:
            continue
        if (d / "result.json").exists():
            raise SystemExit("use a fresh fixture directory: " + str(d))
        run = ["build/scratch/network-load-balance", "config"]
        if a.image:
            refcmd = ["docker", "run", "--rm", "--network=none", "--user", f"{os.getuid()}:{os.getgid()}",
                      "-v", f"{ref}:/src:ro", "-v", f"{fixtures}:/cases", "-w", "/cases/" + d.name,
                      "-e", "LD_LIBRARY_PATH=/src/build", "-e", "TWINS_TXTRACE=reference.tx",
                      a.image, "/src/" + run[0], "config"]
        else:
            refcmd = [str(ref / run[0]), "config"]
        baseline_wall = execute(refcmd, d, dict(os.environ, TWINS_TXTRACE="reference.tx",
                                LD_LIBRARY_PATH=str(ref / "build")), d / "reference.log")
        simcmd = [str(twins), "config", "--topo=../graph", "--flows=flows.csv", "--out=twins.fct",
                  "--txtrace=twins.tx", "--from=2000000000", "--until=2002000000"]
        simwall = execute(simcmd, d, os.environ, d / "twins.log")
        ids = flow_ids(d / "flows.csv")
        truth = []
        for line in (d / "reference.fct").read_text().splitlines():
            row = list(map(int, line.split()))
            truth.append([ids[tuple(row[:6])], row[5], row[6]])
        actual = [list(map(int, line.split())) for line in (d / "twins.fct").read_text().splitlines()]
        expected, observed = wire(d / "reference.tx"), wire(d / "twins.tx")
        diff = next(((i,x,y) for i,(x,y) in enumerate(zip_longest(expected, observed)) if x != y), None)
        log = (d / "twins.log").read_text()
        flags = re.findall(r"undecided ties (\d+), on lost tails (\d+) \| overflows: tx (\d+) "
                           r"queue (\d+) ackq (\d+) qps (\d+) due (\d+)", log)
        clean = bool(flags and all(v == "0" for v in flags[-1]) and "warning:" not in log and "error:" not in log)
        ok = clean and len(ids) == len(truth) and sorted(actual) == sorted(truth) and diff is None
        result = dict(reference_wall_ns=baseline_wall, twins_wall_ns=simwall, reference_command=refcmd,
                      command=simcmd, flows=len(ids), matched_flows=sum((Counter(map(tuple,actual)) &
                      Counter(map(tuple,truth))).values()), wire_reference=len(expected), wire_twins=len(observed),
                      first_difference=diff, terminal_clean=clean, accepted=ok,
                      binary_sha256=hashlib.sha256(twins.read_bytes()).hexdigest())
        (d / "result.json").write_text(json.dumps(result, indent=2))
        print(d.name, result["matched_flows"], "/", len(ids), "flows; first wire difference", diff, flush=True)
        if not ok:
            failures.append(d.name)
    if failures:
        raise SystemExit("failed: " + ", ".join(failures))


if __name__ == "__main__":
    main()
