"""Compare the frozen lossy IRN run with the independent author model."""
import argparse
import csv
import json
import re
from collections import defaultdict
from itertools import zip_longest
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("flows", type=Path)
parser.add_argument("native", type=Path)
parser.add_argument("observed", type=Path)
parser.add_argument("--stock", type=Path)
parser.add_argument("--old", type=Path)
args = parser.parse_args()
with args.flows.open() as source:
    flows = [tuple(int(row[key]) for key in ("src", "dst", "flow_size", "start_ns"))
             for row in csv.DictReader(source)]
identity = {row: index for index, row in enumerate(flows)}
assert len(identity) == len(flows) == 127


def native_fct(path):
    completed = {}
    for line in path.read_text().splitlines():
        src, dst, _, _, size, start, duration, _ = map(int, line.split())
        index = identity[src, dst, size, start]
        assert index not in completed
        completed[index] = [start, duration]
    return completed


def packets(path):
    ports = defaultdict(list)
    for line in path.read_text().splitlines():
        row = line.split()
        assert len(row) == 14
        ports[tuple(map(int, row[1:3]))].append(tuple(row))
    return ports


gold = native_fct(args.native / "reference.fct")
wire = packets(args.native / "wire.raw")
assert len(gold) == 127 and sum(map(len, wire.values())) == 76742
if args.stock:
    assert native_fct(args.stock / "reference.fct") == gold
    assert packets(args.stock / "wire.raw") == wire

native_log = (args.native / "run.log").read_text()
groups = [tuple(map(int, row)) for row in re.findall(r"^NATIVE_GROUP (\d+) (\d+) (\d+) (\d+)", native_log, re.M)]
installed = [tuple(map(int, row)) for row in re.findall(r"^NATIVE_ROOT (\d+) (\d+) (\d+) (\d+) (\d+) (\d+)", native_log, re.M)]
assert len(installed) == 127
order = sorted(range(len(flows)), key=lambda row: flows[row][-1])
assert [(r[1], r[2], r[-1]) for r in installed] == [
    (flows[row][0], flows[row][1], flows[row][-1]) for row in order]
assert [r[0] for r in installed] == list(range(127))
assert [r[4] for r in installed] == list(range(100, 227))
first_group = groups[0]
assert first_group[:3] == (0, 0, 2000000001)
assert [i[1] for i in installed[:2]] == [3, 84]


def verify(candidate, expected_rc):
    completed = {}
    for line in (candidate / "flows.fct").read_text().splitlines():
        index, start, duration = map(int, line.split())
        assert index not in completed
        completed[index] = [start, duration]
    if completed != gold:
        different = sorted(i for i in gold.keys() | completed.keys() if gold.get(i) != completed.get(i))
        first = different[0]
        raise ValueError(f"flow {first}: expected {gold.get(first)}, observed {completed.get(first)}")
    observed = packets(candidate / "wire.raw")
    for port in wire.keys() | observed.keys():
        for index, (a, b) in enumerate(zip_longest(wire.get(port, []), observed.get(port, []))):
            assert a == b, (port, index, a, b)
    log = (candidate / "run.log").read_text()
    assert json.loads((candidate / "receipt.json").read_text())["rc"] == expected_rc
    assert "drops 10569 marks 14278" in log
    assert "txtrace complete: 76742 records" in log
    assert re.search(r"undecided ties 0, on lost tails 0 \| overflows: tx 0 queue 0 ackq 0 qps 0 due 0", log)
    flagged = "undecided root order: 1 comparisons" in log
    assert flagged == (expected_rc == 3)


verify(args.observed, 0)
if args.old:
    verify(args.old, 3)
gpu_log = (args.observed / "run.log").read_text()
root_keys = [tuple(map(int, row)) for row in re.findall(r"^RDMA packet root 0 (\d+) (\d+) (\d+) (\d+)", gpu_log, re.M)]
assert len(root_keys) == len(installed)
assert root_keys[:2] == [(1, 2000000001, 2000000001, 77306572), (2, 2000000001, 2000000001, 77306572)]
leads = {at: at - creator for _, creator, at, _ in groups}
assert all((index, at, lead) == (row[0] + 1, row[-1], leads[row[-1]])
           for (index, at, lead, _), row in zip(root_keys, installed))
result = dict(flows=len(gold), wire_rows=sum(map(len, wire.values())), model_drops=10569, ecn_marks=14278,
              old_flagged=args.old is not None, candidate_rc=0, first_group_uid=first_group[-1],
              first_group_lead_ns=2000000001, first_sources=[3, 84], packed_lead_code=77306572,
              installed_roots=len(installed), scheduled_groups=len(groups))
(args.observed / "acceptance.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
