"""Run the frozen TCP option correctness matrix and same-host timing pairs."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from compare import flows
from native import ARMS, digest


TRACE_LIMIT = 300000000000


def packet_cases(inputs):
    yield "packet-ecmp", "newreno", inputs / "packet.csv", None, 200, True, False
    for name, queue in (("reorder", 200), ("loss", 3)):
        yield name, "newreno", inputs / "one.csv", inputs / "two-paths.graph", queue, True, True
    for cc in ("dctcp", "cubic"):
        yield cc, cc, inputs / "128.csv", inputs / "128.graph", 200, False, False


def gpu_args(case, arm):
    _, cc, offered, graph, queue, spray, _ = case
    sack, timestamps = ARMS[arm]
    args = [f"--flows={offered}", f"--tcp={cc}", f"--queue={queue}", f"--spray={int(spray)}",
            f"--tcp-sack={sack}", f"--tcp-timestamps={timestamps}"]
    if graph:
        counts = Counter(endpoint for line in graph.read_text().splitlines() if line.startswith("link ")
                         for endpoint in line.split()[1:3] if endpoint.startswith("s"))
        args += ["--topo=graph", f"--topofile={graph}", f"--graph_k={max(counts.values())}"]
    else:
        args.append("--k=8")
    return args


def invoke(command, folder, gpu_sample=False):
    folder.mkdir(parents=True, exist_ok=False)
    peak = []
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            proc = subprocess.run(["nvidia-smi", "--query-compute-apps=used_gpu_memory",
                                   "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3)
            memory = [int(word) for word in proc.stdout.splitlines() if word.isdigit()]
            if memory:
                peak.append(sum(memory))
            stop.wait(0.15)

    with (folder / "run.log").open("wb") as log:
        monitor = threading.Thread(target=sample) if gpu_sample else None
        if monitor:
            monitor.start()
        start = time.monotonic_ns()
        try:
            rc = subprocess.run(command, cwd=folder, stdout=log, stderr=subprocess.STDOUT, timeout=1800).returncode
        except subprocess.TimeoutExpired:
            rc = 124
        finally:
            wall = time.monotonic_ns() - start
            stop.set()
            if monitor:
                monitor.join()
    text = (folder / "run.log").read_text()
    loop = re.search(r"Simulation Duration: ([\d.]+)s", text) if gpu_sample else None
    if loop is None:
        loop = re.search(r"\bwall ([\d.]+) s", text)
    row = dict(command=list(map(str, command)), rc=rc, wall_ns=wall, loop_s=float(loop[1]) if loop else None,
               gpu_peak_mib=max(peak) if peak else None, samples=len(peak))
    (folder / "rc").write_text(f"{rc}\n")
    (folder / "receipt.json").write_text(json.dumps(row, indent=2) + "\n")
    return row


def signature(path):
    rows = flows(path)
    value = hashlib.sha256()
    for row in rows:
        value.update((",".join(map(str, row)) + "\n").encode())
    return dict(count=len(rows), completed=sum(row[-1] >= 0 for row in rows), sha256=value.hexdigest())


def freeze(native, destination):
    def native_dir(name, arm):
        if name == "packet-ecmp":
            return native / ("qtrace" if arm in ("off", "sack") else "qtrace-fragments") / arm
        if name in ("loss", "reorder"):
            return native / "directed" / name / arm
        return native / "mixed" / ("Dctcp" if name == "dctcp" else "Cubic") / arm

    def one(key):
        name, arm = key
        path = native_dir(name, arm)
        if json.loads((path / "receipt.json").read_text())["rc"]:
            raise ValueError(f"reference did not succeed in {path}")
        canonical = path / "queue.txt"
        if not canonical.is_file():
            raw = path / "queue.unsorted"
            with (path / "run.log").open() as log, raw.open("w") as stream:
                for line in log:
                    if line.startswith("Q "):
                        stream.write(line[2:])
            with canonical.open("wb") as stream:
                subprocess.run(["sort", "-s", "-k1,1n", "-k2,2n", "-k3,3n", raw], stdout=stream,
                               env={**os.environ, "LC_ALL": "C"}, check=True)
            raw.unlink()
        return f"{name}/{arm}", dict(flows=signature(path / "flows.csv"), queue=digest(canonical))

    with ThreadPoolExecutor(max_workers=8) as executor:
        manifest = dict(executor.map(one, [(case[0], arm) for case in packet_cases(native) for arm in ARMS]))
    destination.write_text(json.dumps(manifest, indent=2) + "\n")


def clean(folder):
    log = (folder / "run.log").read_text()
    labels = ("tx-cap", "active-ovf", "ooo-ovf", "due-ovf", "rtt-ovf", "ring-ovf", "undecided-ties")
    for label in labels:
        hits = re.findall(rf"\b{label} (\d+)", log)
        if not hits or int(hits[-1]):
            return False
    return "fragment-ovf 0" in log or "fragment-ovf" not in log


def matrix(binary, inputs, expect, output):
    report = {}
    for case in packet_cases(inputs):
        name, *_, details = case
        for arm in ARMS:
            folder = output / name / arm
            cmd = [binary, *gpu_args(case, arm), "--out=flows.csv", "--qtrace=queue.txt",
                   f"--qtrace_until={TRACE_LIMIT}"]
            if details:
                cmd += ["--qtrace-options", "--drain"]
            row = invoke(cmd, folder)
            expected = expect[f"{name}/{arm}"]
            flow = folder / "flows.csv"
            queue = folder / "queue.txt"
            found = dict(flows=signature(flow) if flow.is_file() else None,
                         queue=digest(queue) if queue.is_file() else None)
            row.update(found=found, expected=expected, clean=clean(folder))
            row["accepted"] = row["rc"] == 0 and row["clean"] and expected == found
            report[f"{name}/{arm}"] = row
            print(name, arm, row["accepted"], flush=True)
    (output / "acceptance.json").write_text(json.dumps(report, indent=2) + "\n")
    return all(row["accepted"] for row in report.values())


def benchmark(binary, reference, inputs, output):
    case = "timing", "dctcp", inputs / "1024.csv", inputs / "1024.graph", 200, False, False
    results = []
    ineligible = set()
    for trial in range(4):
        arms = list(ARMS)
        for arm in arms[trial:] + arms[:trial]:
            if arm in ineligible:
                continue
            sack, ts = ARMS[arm]
            folder = output / str(trial) / arm
            ref = [reference, "--threads=0", "--topo=file", f"--topofile={case[3]}", f"--flows={case[2]}",
                   "--tcp=ns3::TcpDctcp", "--stop=0.1", "--hostq=red", "--flowEcmp=1", "--queue=200",
                   f"--tcp-sack={sack}", f"--tcp-timestamps={ts}", "--out=flows.csv"]
            gpu = [binary, *gpu_args(case, arm), "--out=flows.csv"]
            commands = (("reference", ref), ("gpu", gpu))
            if trial % 2:
                commands = commands[::-1]
            values = {side: invoke(command, folder / side, gpu_sample=(side == "gpu"))
                      for side, command in commands}
            for side in values:
                file = folder / side / "flows.csv"
                values[side]["flows"] = signature(file) if file.is_file() else None
            gold = values["reference"]["flows"]
            passed = (bool(gold) and gold["count"] > 0 and gold["count"] == gold["completed"] and
                      gold == values["gpu"]["flows"] and clean(folder / "gpu") and all(v["rc"] == 0 for v in values.values()))
            results.append(dict(trial=trial, arm=arm, accepted=passed, **values))
            (output / "timing.json").write_text(json.dumps(results, indent=2) + "\n")
            print("timing", trial, arm, passed, flush=True)
            if not passed:
                ineligible.add(arm)
    return not ineligible


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    for mode in ("matrix", "timing"):
        command = modes.add_parser(mode)
        for name in ("binary", "inputs", "out", "reference"):
            command.add_argument(name, type=lambda value: Path(value).resolve())
    command = modes.add_parser("freeze")
    command.add_argument("native", type=Path)
    command.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.mode == "freeze":
        freeze(args.native, args.destination)
        accepted = True
    elif args.mode == "matrix":
        accepted = matrix(args.binary, args.inputs, json.loads(args.reference.read_text()), args.out)
    else:
        accepted = benchmark(args.binary, args.reference, args.inputs, args.out)
    raise SystemExit(0 if accepted else 1)
