"""Compare flow outputs and ordered histories of individual devices."""
import argparse
from collections import defaultdict
import csv
from itertools import zip_longest
import json
from pathlib import Path
import re


def flows(path):
    with path.open(newline="") as stream:
        return [tuple(map(int, row[:6])) for row in list(csv.reader(stream))[1:]]


def queue(path, reference):
    rows = defaultdict(list)
    with path.open() as stream:
        for line in stream:
            if reference:
                if not line.startswith("Q "):
                    continue
                line = line[2:]
            stamp, node, device, src, dst, kind, sequence, occupancy, *detail = line.split()
            rows[int(node), int(device)].append((int(stamp), int(src), int(dst), kind, int(sequence), int(occupancy), *detail))
    return rows


def compare(reference, gpu):
    expected = flows(reference / "flows.csv")
    actual = flows(gpu / "flows.csv")
    first_flow = next(((i, a, b) for i, (a, b) in enumerate(zip_longest(expected, actual)) if a != b), None)
    left = queue(reference / "run.log", True)
    right = queue(gpu / "queue.txt", False)
    first = None
    mismatching_queues = 0
    for key in left.keys() | right.keys():
        a, b = left.get(key, []), right.get(key, [])
        if a == b:
            continue
        mismatching_queues += 1
        for i, (x, y) in enumerate(zip_longest(a, b)):
            if x != y:
                stamp = min(row[0] for row in (x, y) if row is not None)
                difference = (stamp, key, i, x, y)
                if first is None or difference[:3] < first[:3]:
                    first = difference
                break
    log = (gpu / "run.log").read_text()
    with (reference / "run.log").open() as stream:
        ref_summary = next((line for line in stream if line.startswith("flows ")), "")
    fields = ("tx-cap", "active-ovf", "ooo-ovf", "due-ovf", "rtt-ovf", "ring-ovf", "undecided-ties")
    flags = {field: list(map(int, re.findall(rf"\b{field} (\d+)", log))) for field in fields}
    counters = {}
    for field in ("retx", "rto", "drops", "marks"):
        searched = [re.search(rf"\b{field} (\d+)", text) for text in (ref_summary, log)]
        counters[field] = [int(item[1]) if item else None for item in searched]
    terminal_clean = all(values and values[-1] == 0 for values in flags.values())
    ref_records = sum(map(len, left.values()))
    gpu_records = sum(map(len, right.values()))
    accepted = (bool(actual) and all(row[-1] >= 0 for row in actual) and first_flow is None and
                ref_records > 0 and first is None and terminal_clean and
                counters["retx"][0] == counters["retx"][1] and int((gpu / "rc").read_text()) == 0)
    return dict(accepted=accepted, reference_flows=len(expected), gpu_flows=len(actual), first_flow=first_flow,
                reference_queue_records=ref_records, gpu_queue_records=gpu_records, first_queue=first,
                mismatching_queues=mismatching_queues, flags=flags, counters=counters)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", type=Path)
    parser.add_argument("gpu", type=Path)
    args = parser.parse_args()
    report = compare(args.reference, args.gpu)
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["accepted"] else 1)
