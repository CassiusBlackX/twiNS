"""Validate the optimized RX ordinary entry and four original packet/timer audits."""
import argparse
import csv
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("run", type=Path)
parser.add_argument("frozen", type=Path)
args = parser.parse_args()
root = args.run
frozen = args.frozen


def flows(path):
    with path.open() as stream:
        reader = csv.reader(stream)
        next(reader)
        return [row[:6] for row in reader]


def timers(path):
    hosts = defaultdict(list)
    for line in path.read_text().splitlines():
        if line.startswith("TCP_TIMER "):
            _, host, *event = line.split()
            hosts[int(host)].append(event)
    return hosts


def clean(log):
    final = next(line for line in log.splitlines() if line.startswith("flows "))
    counters = list(map(int, re.findall(r"(?:tx-cap|active-ovf|ooo-ovf|due-ovf|rtt-ovf|ring-ovf|undecided-ties|fragment-timer-ovf) (\d+)", final)))
    assert len(counters) == 8 and not any(counters), final
    assert "IPv4 fragment-ovf 0" in log


gold = frozen / "storage-hip-release/default/flows.csv"
assert hashlib.sha256(gold.read_bytes()).hexdigest() == "e198737c5ddc7bc86c2f74a989d8a4946fd9e0c23626cca7301e17c9e80f3501"
reference = flows(gold)
assert len(reference) == 10240 and all(row[-1] != "-1" for row in reference)

samples = defaultdict(list)
for trial in range(1, 4):
    run = root / f"tcp-hip-{trial}"
    data = json.loads((run / "summary.json").read_text())
    for name in ("default", "preset"):
        record = data[name]
        d = run / name
        log = (d / "run.log").read_text()
        assert record["rc"] == 0 and flows(d / "flows.csv") == reference
        assert 0 < record["peak_kib"] < 16 * 1024**2
        assert "TCP split arrival allocation:" in log
        if name == "default":
            assert "TCP arrival attempt cap=4096 observed=" in log and "tail_cap=8192" in log
        clean(log)
        samples[name].append(dict(trial=trial, wall_s=record["wall_ns"] / 1e9,
                                  peak_gib=record["peak_kib"] / 1024**2,
                                  loop_s=float(re.search(r"^Simulation Duration: ([\d.]+)s", log, re.M)[1])))
    if trial == 1:
        negative = data["forced-fixed"]
        note = (run / "forced-fixed/run.log").read_text()
        assert negative["rc"] != 0 and "TCP arrival attempt cap=1 " in note
        assert data["forced-auto"]["rc"] == 0 and flows(run / "forced-auto/flows.csv") == reference
        clean((run / "forced-auto/run.log").read_text())

audit = {}
for backend in ("hip", "cuda"):
    results = {}
    for arm in ("dctcp-0", "dctcp-1", "cubic-0", "cubic-1"):
        ref = frozen / "tcp-mixed-o0-hip" / f"{arm}-split"
        case = root / f"tcp-audit-{backend}" / f"{arm}-split"
        assert (case / "rc").read_text().strip() == "0"
        assert flows(case / "flows.csv") == flows(ref / "flows.csv")
        trace = (case / "queue.txt").read_bytes()
        assert trace == (ref / "queue.txt").read_bytes()
        logs = case / "run.log"
        clean(logs.read_text())
        schedule = timers(logs)
        assert schedule and schedule == timers(ref / "run.log")
        results[arm] = dict(flows=len(flows(case / "flows.csv")), queue_rows=trace.count(b"\n"),
                            timer_rows=sum(map(len, schedule.values())))
    audit[backend] = results

result = dict(matched_flows=len(reference), samples=samples, audits=audit, fixed_capacity_rejected=True)
(root / "tcp-acceptance.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
