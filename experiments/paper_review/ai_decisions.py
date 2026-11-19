"""Frozen 24-cell comparison of PFC partitions and recorded last-trigger replay."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import time

from causal_dag import dag


WORKLOADS = {
    "moe": (16, "moe_16.txt", (16, 32)),
    "a2a": (128, "paper/c_alltoall_64m_128.txt", (16,)),
}


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def freeze(original, out):
    inputs = out/"inputs"
    inputs.mkdir(parents=True)
    shutil.copyfile(original/"validation/simai/SimAI.conf", inputs/"SimAI.conf")
    cases = []
    for name, (size, workload, budgets) in WORKLOADS.items():
        shutil.copyfile(original/"out/simai/wl"/workload, inputs/f"{name}.txt")
        for stem in ("sx", "g200"):
            suffix = "_200" if stem == "g200" else ""
            shutil.copyfile(original/"data/simai"/f"sx{size}{suffix}.topo", inputs/f"{name}-{stem}.topo")
            for ext in ("dag", "ref"):
                shutil.copyfile(original/"out/simai/whatif"/f"{name}_{stem}.{ext}", inputs/f"{name}-{stem}.{ext}")
            for budget in budgets:
                for mmu in ("shared", "fixed"):
                    for frontend in ("live", "replay"):
                        cases.append(dict(workload=name, profile=stem, budget=budget, mmu=mmu, frontend=frontend,
                                          id=f"{name}-{stem}-{budget}-{mmu}-{frontend}"))
    plan = dict(cases=cases, choice="minimum full message makespan at each shared-buffer budget",
                replay="original 32 MiB Spectrum-X last-trigger DAG for each workload",
                hashes={p.name: sha(p) for p in sorted(inputs.iterdir())})
    (out/"plan.json").write_text(json.dumps(plan, indent=2)+"\n")


def simulate(out, rdma, ai):
    inputs = out/"inputs"
    plan = json.loads((out/"plan.json").read_text())
    for name, fingerprint in plan["hashes"].items():
        if sha(inputs/name) != fingerprint:
            raise ValueError(f"frozen input changed: {name}")
    for case in plan["cases"]:
        work, profile = case["workload"], case["profile"]
        folder = out/"runs"/case["id"]
        folder.mkdir(parents=True, exist_ok=True)
        if (folder/"receipt.json").exists():
            continue
        live = case["frontend"] == "live"
        command = [str(ai if live else rdma), str(inputs/"SimAI.conf"),
                   f"--topo={inputs}/{work}-{profile}.topo", f"BUFFER_SIZE={case['budget']}",
                   f"TWINS_FIXED_PFC={int(case['mmu'] == 'fixed')}", "--out=messages.fct"]
        if live:
            command += [f"--astra={inputs}/{work}.txt", "--dag-out=actions.dag"]
        else:
            command += [f"--flows={inputs}/{work}-sx.dag"]
        start = time.monotonic_ns()
        with (folder/"gpu.log").open("w") as log:
            try:
                rc = subprocess.run(command, cwd=folder, stdout=log, stderr=subprocess.STDOUT, timeout=300).returncode
            except subprocess.TimeoutExpired:
                rc = 124
        receipt = folder/"receipt.tmp"
        receipt.write_text(json.dumps(dict(command=command, returncode=rc,
                                           wall_ns=time.monotonic_ns()-start))+"\n")
        receipt.replace(folder/"receipt.json")


def message_dependencies(path):
    ids, parents, delays, occurrences = {}, {}, {}, Counter()
    for line in path.read_text().splitlines():
        tokens = line.split()
        if not tokens:
            continue
        if tokens[0] == "flow":
            fields = tuple(map(int, tokens[2:5]))
            ids[tokens[1]] = fields + (occurrences[fields],)
            occurrences[fields] += 1
        elif tokens[0] == "on":
            parents[tokens[1]] = tokens[2]
        elif tokens[0] == "timer":
            delays[tokens[1]] = int(tokens[2])
    dependencies = {}
    for fid, identity in ids.items():
        parent, delay = parents["f"+fid], 0
        while parent.startswith("t"):
            delay += delays[parent[1:]]
            parent = parents[parent]
        dependencies[identity] = (parent[0], ids.get(parent[1:]), delay)
    return ids, dependencies


def changed_dependency(first, second, second_dag):
    one_ids, one_deps = message_dependencies(first/"actions.dag")
    two_ids, two_deps = message_dependencies(second_dag)

    def read_fct(path, ids):
        rows = (line.split() for line in path.read_text().splitlines())
        return {ids[fid]: (int(start), int(duration)) for fid, start, duration in rows}

    a, b = read_fct(first/"messages.fct", one_ids), read_fct(second/"messages.fct", two_ids)
    changed = [key for key in a.keys() & b.keys() if one_deps[key] != two_deps[key]]
    moving = [key for key in changed if a[key][0] != b[key][0]]
    first = min(moving, key=lambda key: (min(a[key][0], b[key][0]), key), default=None)
    witness = dict(message=first, first_model=one_deps[first], second_model=two_deps[first],
                   start_ns=[a[first][0], b[first][0]]) if first is not None else None
    return dict(changed_last_trigger_count=len(changed), changed_start_count=len(moving), earliest=witness)


def report(out):
    inputs = out/"inputs"
    results = []
    for case in json.loads((out/"plan.json").read_text())["cases"]:
        folder = out/"runs"/case["id"]
        receipt = folder/"receipt.json"
        if not receipt.exists():
            continue
        code = json.loads(receipt.read_text())["returncode"]
        messages = folder/"messages.fct"
        log = (folder/"gpu.log").read_text()
        counters = re.search(r"drops (\d+) marks (\d+) pauses (\d+) resumes (\d+) nacks (\d+)", log)
        pass_ends = [int(t) for t in re.findall(r"all passes finished at time: (\d+)", log)]
        expected = (inputs/f"{case['workload']}-sx.ref").read_text().splitlines()
        rows = [tuple(map(int, line.split())) for line in messages.read_text().splitlines()] if messages.exists() else []
        makespan = max((row[1]+row[2] for row in rows), default=None)
        clean = "undecided ties 0, on lost tails 0 | overflows: tx 0 queue 0 ackq 0 qps 0 due 0" in log
        unique = {row[0] for row in rows}
        accepted = code == 0 and len(rows) == len(unique) == len(expected) and clean and counters is not None
        anchor = None
        if case["mmu"] == "shared" and case["frontend"] == "live":
            profile = f"{case['workload']}-{case['profile']}"
            ref = inputs/profile if case["budget"] == 32 else out/"native16"/profile/"run"
            if ref.with_suffix(".ref").exists() and rows:
                reference = ref.with_suffix(".ref").read_text().splitlines()
                anchor = dict(messages=set(rows) == {tuple(map(int, line.split())) for line in reference},
                              causal_dag=dag(ref.with_suffix(".dag"), normalize_main_order=True) ==
                              dag(folder/"actions.dag", normalize_main_order=True))
                accepted &= all(anchor.values())
        results.append(dict(**case, accepted=accepted, code=code, completed=len(rows), expected=len(expected),
                            makespan_ns=makespan, matches_native_anchor=anchor,
                            astra_pass_end_ns=max(pass_ends, default=None),
                            counters=list(map(int, counters.groups())) if counters else None))
    choices = {}
    for row in results:
        key = "/".join(str(row[field]) for field in ("workload", "budget", "mmu", "frontend"))
        choices.setdefault(key, []).append(row)
    winners = {key: min(rows, key=lambda row: (row["makespan_ns"], row["profile"]))["profile"]
               for key, rows in choices.items() if len(rows) == 2 and all(row["accepted"] for row in rows)}
    effects, dependencies, replay = {}, {}, {}
    for key, winner in winners.items():
        work, budget, mmu, frontend = key.split("/")
        truth = choices[f"{work}/{budget}/shared/live"]
        if len(truth) == 2 and all(row["accepted"] for row in truth):
            costs = {row["profile"]: row["makespan_ns"] for row in truth}
            effects[key] = dict(live_shared_400_minus_200_ns=costs["sx"]-costs["g200"],
                                regret_ns=costs[winner]-min(costs.values()))
        if mmu == "fixed" and frontend == "live" and len(truth) == 2:
            for one in truth:
                other = next(row for row in choices[key] if row["profile"] == one["profile"])
                if one["accepted"]:
                    first, second = (out/"runs"/row["id"] for row in (one, other))
                    dependencies[f"{work}/{budget}/{one['profile']}"] = changed_dependency(first, second, second/"actions.dag")
        if frontend == "replay":
            live = choices[f"{work}/{budget}/{mmu}/live"]
            for one in live:
                other = next(row for row in choices[key] if row["profile"] == one["profile"])
                if one["accepted"]:
                    first, second = (out/"runs"/row["id"] for row in (one, other))
                    replay[f"{work}/{budget}/{mmu}/{one['profile']}"] = changed_dependency(first, second, inputs/f"{work}-sx.dag")
    return dict(runs=results, choices=winners, choice_effects=effects, fixed_mmu_dependencies=dependencies,
                recorded_dependencies=replay)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("freeze", "simulate", "report"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--original", type=Path)
    parser.add_argument("--rdma", type=Path)
    parser.add_argument("--ai", type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    if args.mode == "freeze":
        freeze(args.original.resolve(), out)
    elif args.mode == "simulate":
        simulate(out, args.rdma.resolve(), args.ai.resolve())
    else:
        print(json.dumps(report(out), indent=2))
