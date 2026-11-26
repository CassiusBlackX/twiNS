"""Run and compare the prescribed path transition against the author model."""
import argparse
import json
import os
import re
import subprocess
import time
from collections import defaultdict
from itertools import zip_longest
from pathlib import Path


def run(args):
    args.output.mkdir()
    for mode in ("control", "tail", "timeout", "capacity"):
        trials = 1 if mode == "capacity" else args.trials
        for trial in range(trials):
            work = args.output / (mode if trial == 0 else f"{mode}-{trial}")
            work.mkdir()
            control = mode == "control"
            limit = 1 if mode == "capacity" else 4096
            command = [
                args.binary, args.inputs / "conweave.cfg", f"--topo={args.inputs / 'fabric.graph'}",
                f"--flows={args.inputs / 'flows.csv'}", "--out=flows.fct",
                "--stop-ns=2001000000", "IRN_BDP=104000", "--fp-bases=1000003,1000033",
                f"--conweave-voq=0,2,3,2,{2 if control else 3},{4294967295 if control else 2000}",
                f"--conweave-voq-cap={limit}",
            ]
            env = dict(os.environ)
            if args.trace:
                env["TWINS_CW_TRACE"] = "1"
                command.extend(("--txtrace=wire.raw", "--txtrace-cap=10000"))
            if mode == "timeout":
                command.append("--conweave-drop-tail")
            start = time.monotonic_ns()
            with (work / "run.log").open("w") as stream:
                rc = subprocess.run(["timeout", "120", *command], cwd=work, env=env,
                                    stdout=stream, stderr=subprocess.STDOUT).returncode
            (work / "run.json").write_text(json.dumps(dict(command=list(map(str, command)),
                    rc=rc, wall_s=(time.monotonic_ns() - start) / 1e9), indent=2) + "\n")
            print(work.name, rc, flush=True)


def inventory(path, cutoff):
    ports = defaultdict(list)
    for line in path.read_text().splitlines():
        fields = line.split()
        if int(fields[0]) < cutoff:
            ports[tuple(fields[1:3])].append(fields)
    return dict(ports)


def actions(path):
    nodes = defaultdict(list)
    for line in path.read_text().splitlines():
        fields = line.split()
        if fields and fields[0] == "CW":
            nodes[fields[2]].append(fields[1:])
    return dict(nodes)


def compare(args):
    results = []
    for work in sorted(args.output.iterdir()):
        if not work.is_dir():
            continue
        receipt = json.loads((work / "run.json").read_text())
        log = (work / "run.log").read_text()
        summary = re.search(r"ConWeave VOQ: high-water (\d+), flushes (\d+), pending (\d+), overflow (\d+)", log)
        mode = work.name.split("-")[0]
        if not summary:
            results.append(dict(mode=work.name, rc=receipt["rc"], matched=False))
            continue
        high, flushed, pending, overflow = map(int, summary.groups())
        result = dict(mode=work.name, wall_s=receipt["wall_s"], rc=receipt["rc"],
                      high_water=high, flushes=flushed, pending=pending, overflow=overflow)
        if mode == "capacity":
            result["matched"] = receipt["rc"] == 3 and overflow > 0
        else:
            ref = args.inputs / mode
            gold = [[str(i), line.split()[5], line.split()[6]]
                    for i, line in enumerate((ref / "reference.fct").read_text().splitlines())]
            candidate = [line.split() for line in (work / "flows.fct").read_text().splitlines()]
            result["fct"] = dict(expected=gold, observed=candidate)
            result["matched"] = bool(gold) and gold == candidate and (
                receipt["rc"] == pending == overflow == 0 and
                (high > 0 and flushed == 1 if mode != "control" else high == flushed == 0))
            if (work / "wire.raw").exists():
                want = inventory(ref / "wire.raw", 2001000000)
                got = inventory(work / "wire.raw", 2001000000)
                ref_actions, gpu_actions = (actions(p / "run.log") for p in (ref, work))
                result.update(wire_rows=sum(map(len, want.values())),
                              wire_match=bool(want) and want == got,
                              state_match=bool(ref_actions) and ref_actions == gpu_actions)
                if not result["wire_match"]:
                    for key in sorted(want.keys() | got.keys()):
                        if want.get(key) != got.get(key):
                            result["first_wire_difference"] = [key, next(
                                [i, x, y] for i, (x, y) in enumerate(zip_longest(
                                    want.get(key, []), got.get(key, []))) if x != y)]
                            break
                if not result["state_match"]:
                    result["state_difference"] = dict(expected=ref_actions, observed=gpu_actions)
                result["matched"] &= result["wire_match"] and result["state_match"]
        results.append(result)
    (args.output / "comparison.json").write_text(json.dumps(results, indent=2) + "\n")
    return int(not results or any(not row["matched"] for row in results))


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("operation", choices=("run", "compare"))
parser.add_argument("output", type=lambda s: Path(s).resolve())
parser.add_argument("--binary", type=lambda s: Path(s).resolve())
parser.add_argument("--inputs", type=lambda s: Path(s).resolve(), required=True)
parser.add_argument("--trace", action="store_true")
parser.add_argument("--trials", type=int, default=1)
args = parser.parse_args()
if args.operation == "run":
    run(args)
else:
    raise SystemExit(compare(args))
