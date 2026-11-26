"""Create, run and compare the 32/48/64-port TCP experiments."""
import argparse
import csv
import json
import os
import re
import subprocess
import time
from collections import defaultdict
from pathlib import Path

STOP_NS = 2_000_000
SPINES = 16
LEAVES = 4


def generate(output):
    output.mkdir(parents=True, exist_ok=False)
    for ports in (32, 48, 64):
        hosts_per_leaf = ports - SPINES
        hosts = LEAVES * hosts_per_leaf
        for timing in (("uniform", "mixed") if ports == 64 else ("uniform",)):
            case = output / f"{ports}-{timing}"
            case.mkdir()
            with (case / "fabric.graph").open("w") as topo:
                print(f"hosts {hosts}\nswitches {LEAVES + SPINES}", file=topo)
                for host in range(hosts):
                    rate = 25 if timing == "mixed" and host % 4 == 0 else 100
                    delay = 500_000 if timing == "mixed" else 1_000_000
                    print(f"link h{host} s{host // hosts_per_leaf} {rate * 10**9} {delay}", file=topo)
                for spine in range(SPINES):
                    for leaf in range(LEAVES):
                        delay = 750_137 if timing == "mixed" and spine % 2 else 1_000_000
                        print(f"link s{leaf} s{LEAVES + spine} 100000000000 {delay}", file=topo)
            flows = []
            for source in range(hosts):
                leaf, port = divmod(source, hosts_per_leaf)
                peer = ((leaf + 1) % LEAVES) * hosts_per_leaf + port
                flows.append((source, peer, 1460 * 14 + (port % 5), 0))
                # Simultaneous ingress to a single egress, itself on a high-index
                # access port on the 64-port leaves.
                if leaf != LEAVES - 1:
                    flows.append((source, hosts - 1, 1460 * 12, 45_000))
            with (case / "flows.csv").open("w") as stream:
                writer = csv.writer(stream, lineterminator="\n")
                writer.writerow(("src", "dst", "flow_size", "start_ns"))
                writer.writerows(flows)


def run(args):
    args.output.mkdir()
    native = args.engine == "native"
    for case in sorted(args.inputs.iterdir()):
        if args.case and case.name not in args.case:
            continue
        for spray in (0, 1):
            for trial in range(args.trials):
                work = args.output / f"{case.name}-{spray}-{trial}"
                work.mkdir()
                trace = args.trace and trial == 0
                common = [
                    f"--topofile={case / 'fabric.graph'}",
                    f"--flows={case / 'flows.csv'}",
                    f"--queue={args.queue}", "--ecnK=8", "--out=flows.csv",
                ]
                if native:
                    cmd = [
                        "--topo=file", "--tcp=ns3::TcpDctcp", "--threads=0", "--hostq=red",
                        f"--flowEcmp={1 - spray}", f"--stop={STOP_NS / 1e9}",
                    ]
                else:
                    slot_ns = 500 if "mixed" in case.name else 1000
                    cmd = [
                        "--topo=graph", "--tcp=dctcp", f"--spray={spray}",
                        "--drain", f"--max_slots={STOP_NS // slot_ns}",
                    ]
                    if args.history:
                        cmd.append(f"--start-history={args.history}")
                env = None
                if trace and native:
                    env = dict(os.environ, TRACE_ALLQ=str(STOP_NS * 1000),
                               DUMP_ROUTES=str(work / "routes.txt"))
                elif trace:
                    cmd.extend(["--qtrace=queue.txt", f"--qtrace_until={STOP_NS * 1000 - 1}",
                                "--dump_routes=routes.txt"])
                command = [str(args.binary), *common, *cmd]
                start = time.monotonic_ns()
                with (work / "run.log").open("w") as log:
                    result = subprocess.run(["timeout", "180", *command], cwd=work, env=env,
                                            stdout=log, stderr=subprocess.STDOUT)
                wall = (time.monotonic_ns() - start) / 1e9
                if trace and native:
                    rows = [line[2:] for line in (work / "run.log").read_text().splitlines()
                            if line.startswith("Q ")]
                    rows.sort(key=lambda line: tuple(map(int, line.split(maxsplit=3)[:3])))
                    (work / "queue.txt").write_text("".join(line + "\n" for line in rows))
                (work / "run.json").write_text(json.dumps(
                    dict(command=command, rc=result.returncode, wall_s=wall, traced=trace), indent=2) + "\n")
                print(work.name, result.returncode, f"{wall:.3f} s", flush=True)


def flow_rows(path):
    with path.open() as stream:
        return [tuple(row[:6]) for row in list(csv.reader(stream))[1:]]


def compare(args):
    rows = []
    failed = False
    for work in sorted(args.output.iterdir()):
        if not work.is_dir():
            continue
        receipt = json.loads((work / "run.json").read_text())
        case, spray, trial = work.name.rsplit("-", 2)
        native = args.binary / f"{case}-{spray}-0"
        if not (work / "flows.csv").exists():
            rows.append(dict(case=case, spray=int(spray), trial=int(trial), rc=receipt["rc"],
                             six_field_match=False, reason="no candidate output"))
            failed = True
            continue
        expected, actual = (flow_rows(p / "flows.csv") for p in (native, work))
        log = (work / "run.log").read_text()
        flags = [int(n) for n in re.findall(
            r"(?:tx-cap|active-ovf|ooo-ovf|due-ovf|rtt-ovf|ring-ovf|undecided-ties|fragment-timer-ovf|fragment-ovf) (\d+)",
            log)]
        has_final_flags = bool(flags)
        row = dict(case=case, spray=int(spray), trial=int(trial), rc=receipt["rc"],
                   wall_s=receipt["wall_s"], rows=len(actual),
                   complete=sum(x[-1] != "-1" for x in actual),
                   six_field_match=expected == actual, zero_flags=has_final_flags and not any(flags))
        if not row["six_field_match"]:
            row["first_flow_difference"] = next(
                ([i, x, y] for i, (x, y) in enumerate(zip(expected, actual)) if x != y), None)
        if (work / "queue.txt").exists():
            gold, observed = ((p / "queue.txt").read_text().splitlines() for p in (native, work))
            row["queue_match"] = observed == gold
            row["queue_rows"] = len(gold)
            if not row["queue_match"]:
                row["first_queue_difference"] = next(
                    ([i, x, y] for i, (x, y) in enumerate(zip(gold, observed)) if x != y),
                    [min(len(gold), len(observed)), len(gold), len(observed)])
            row["route_match"] = (work / "routes.txt").read_text().splitlines() == (
                native / "routes.txt").read_text().splitlines()
            high = defaultdict(int)
            simultaneous = defaultdict(set)
            hosts = (int(case.split("-")[0]) - SPINES) * LEAVES
            for line in gold:
                ts, node, port, source, _, _, _, depth, *_ = line.split()
                if int(node) >= hosts and int(port) > 32:
                    high[(int(node), int(port))] = max(high[(int(node), int(port))], int(depth))
                    simultaneous[(ts, node, port)].add(source)
            row["active_high_ports"] = len(high)
            row["high_max_depth"] = max(high.values(), default=0)
            row["high_same_time_contention"] = sum(len(sources) > 1 for sources in simultaneous.values())
        failed |= (receipt["rc"] != 0 or not row["six_field_match"] or not row["zero_flags"] or
                   row.get("queue_match") is False or row.get("route_match") is False)
        rows.append(row)
    (args.output / "comparison.json").write_text(json.dumps(rows, indent=2) + "\n")
    return int(failed or not rows)


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("engine", choices=("generate", "native", "gpu", "compare"))
parser.add_argument("output", type=lambda s: Path(s).resolve())
parser.add_argument("--binary", type=lambda s: Path(s).resolve())
parser.add_argument("--inputs", type=lambda s: Path(s).resolve())
parser.add_argument("--case", action="append")
parser.add_argument("--trials", type=int, default=1)
parser.add_argument("--queue", type=int, default=24)
parser.add_argument("--history", type=int)
parser.add_argument("--trace", action="store_true")
args = parser.parse_args()
if args.engine == "generate":
    generate(args.output)
elif args.engine == "compare":
    raise SystemExit(compare(args))
else:
    run(args)
