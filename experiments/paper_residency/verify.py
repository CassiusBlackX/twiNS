"""Check the four paper experiments against the frozen reference traces."""
import argparse
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
import re
from statistics import median

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("results", type=Path)
args = parser.parse_args()
root = args.results
old = root / "../boundary-review"
review = root / "../full-review-20261126"
tcp = root / "../tcp-followup-20261126"

def load(path):
    return json.loads(path.read_text())

def sha(path):
    with path.open("rb") as data:
        return hashlib.file_digest(data, "sha256").hexdigest()

def line_count(path):
    with path.open("rb") as data:
        return sum(chunk.count(b"\n") for chunk in iter(lambda: data.read(1 << 20), b""))

def rc(path):
    return int((path / "rc").read_text())

def clean_tcp(log):
    terminal = next(line for line in log.splitlines() if line.startswith("flows ") and "tx-cap" in line)
    keys = r"tx-cap|active-ovf|ooo-ovf|due-ovf|rtt-ovf|ring-ovf|undecided-ties|fragment-timer-ovf"
    return re.findall(rf"(?:{keys}) (\d+)", terminal) == ["0"] * 8

def clean_rdma(log):
    terminal = [line for line in log.splitlines() if "overflows:" in line][-1]
    return bool(re.search(r"undecided ties 0, on lost tails 0 \| overflows: tx 0 queue 0 ackq 0 qps 0 due 0", terminal))

def wire(path, cutoff=10**20):
    ports = defaultdict(list)
    for raw in path.open("rb"):
        tick, node, port, _ = raw.split(maxsplit=3)
        if int(tick) < cutoff:
            ports[(int(node), int(port))].append(raw.strip())
    return ports

def packet_count(ports):
    return sum(map(len, ports.values()))

def rows(path):
    return [tuple(map(int, row.split())) for row in path.read_text().splitlines()]

def tcp_rows(path):
    with path.open() as stream:
        r = csv.reader(stream)
        next(r)
        return [tuple(map(int, line[:6])) for line in r]

def fct_signature(path):
    return hashlib.sha256("".join(",".join(map(str, row)) + "\n" for row in tcp_rows(path)).encode()).hexdigest()

def irn_rows(path, native, flows, cutoff):
    if native:
        return sorted(((r[0], r[1], r[4], r[5]), r[6]) for r in rows(path) if r[5]+r[6] < cutoff)
    with flows.open() as stream:
        reader = csv.reader(stream)
        next(reader)
        inputs = [tuple(map(int,row)) for row in reader]
    result = []
    for flow_id, start, fct in rows(path):
        entry = inputs[flow_id]
        assert start == entry[-1]
        result.append((entry, fct))
    return sorted(result)

def timer_order(path):
    hosts = defaultdict(list)
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("TCP_TIMER "):
            _, host, *fields = line.split()
            hosts[int(host)].append(tuple(map(int, fields)))
    return hosts

def profiles(path, extra):
    results = []
    for case in load(path / "summary.json"):
        d = path / f"{case['trial']}-{case['mode']}"
        log = (d / "run.log").read_text(errors="replace")
        assert case["rc"] == 0 and case["peak_gpu_mib"] > 0 and case["peak_rss_kib"] > 0, d
        results.append({**case, **extra(d, log)})
    return results

def strong(d, log):
    native = review / "native/strong"
    assert clean_rdma(log), d
    assert sorted(rows(d/"output.fct")) == sorted(rows(native/"output.fct")), d
    a = wire(d/"wire.raw")
    b = wire(native/"selected-wire.log")
    assert a == b and packet_count(b) == 31396, d
    phases = [dict(zip(("world_s","initialization_s","graph_s","execution_s"), map(float, match))) for match in
              re.findall(r"world_s=(\S+) initialization_s=(\S+) graph_s=(\S+) execution_s=(\S+)",log)]
    mode = d.name.split("-")[-1]
    attempts = 1 if mode == "preset" else 3
    assert len(phases) == attempts, d
    restores = log.count("restored from the previous attempt")
    assert restores == (2 if mode == "reuse" else 0), d
    total = float(re.findall(r"RDMA total wall including retries: (\S+)",log)[-1])
    return dict(flows=len(rows(d/"output.fct")), wire=packet_count(a), phases=phases, restores=restores,
                internal_wall_s=total, snapshot_s=re.findall(r"RDMA topology snapshot: (\S+)", log))

def tcp_profile(d, log, signature):
    assert clean_tcp(log), d
    assert fct_signature(d/"flows.csv") == signature, d
    return dict(flows=len(tcp_rows(d/"flows.csv")), fct_sha256=signature,
                loop_s=float(re.search(r"Simulation Duration: ([\d.]+)",log)[1]),
                split_allocation=re.findall(r"TCP split arrival allocation: (.*)",log))

def totals(runs):
    out = {}
    for mode in dict.fromkeys(r["mode"] for r in runs):
        matching = [r for r in runs if r["mode"] == mode]
        assert len(matching) == 3
        out[mode] = {key: dict(median=median(v), min=min(v), max=max(v)) for key in
                     ("wall_ns","peak_gpu_mib","peak_rss_kib") if (v := [r[key] for r in matching])}
    return out

result = {}
cubic = root / "short-native"
gold = cubic / "queue.txt"
t = tcp_rows(cubic/"flows.csv")
assert rc(cubic) == 0 and len(t) == 12519
assert line_count(gold) == 5814679
digest = sha(gold)
for backend in ("hip", "cuda"):
    d = root / f"short-{backend}"
    assert rc(d) == 0 and clean_tcp((d/"run.log").read_text())
    assert sha(d/"queue.txt") == digest and tcp_rows(d/"flows.csv") == t, d
result["cubic"] = dict(flows=len(t), queue=line_count(gold), sha256=digest, cutoff_ps=100000000000)

irn = {}
for group, lows in (("original",(100000,500,800,900)),("collision",(833,834,835)),("crossing",(834,))):
    for low in lows:
        key = f"{group}-{low}"
        n = old / ("irn-stock" if group == "original" else "irn-collision-native") / str(low)
        cutoff = 2000004171 if group == "crossing" else 2000100000
        ref_wire = wire(n/"wire.raw",cutoff)
        ref_fct = irn_rows(n/"reference.fct",True,None,cutoff)
        complete = max(start+fct for (_,_,_,start),fct in ref_fct)
        irn[key] = dict(cutoff_ns=cutoff, wire=packet_count(ref_wire), flows=len(ref_fct),
                        after_last_flow=sum(int(r.split(maxsplit=1)[0]) > complete for p in ref_wire.values() for r in p))
        for arch in ("hip","cuda"):
            d = root / f"rdma-{arch}-final" / key
            inputs = d.parent / f"{'collision' if group == 'crossing' else group}.csv"
            assert rc(d) == (2 if group == "crossing" else 0), d
            assert wire(d/"wire.raw") == ref_wire and irn_rows(d/"flows.fct",False,inputs,cutoff) == ref_fct, d
            assert clean_rdma((d/"run.log").read_text()), d
result["irn_cutoff"] = irn
cutoff_cases = {}
native = old / "irn-collision-native/834"
for arch in ("hip", "cuda"):
    dirs = root / f"cutoff-{arch}"
    archived = old / f"irn-collision-{arch}/834"
    for mode in ("default", "drain", "cutoff-equal"):
        d = dirs / mode
        end = 2000004170 if mode == "cutoff-equal" else 2000100000
        ref = wire(archived/"wire.raw") if mode == "default" else wire(native/"wire.raw", end)
        ref_fct = irn_rows(native/"reference.fct", True, None, end)
        assert rc(d) == (2 if mode == "cutoff-equal" else 0), d
        assert clean_rdma((d/"run.log").read_text()), d
        assert wire(d/"wire.raw") == ref and irn_rows(d/"flows.fct", False, dirs/"two.csv", end) == ref_fct, d
        cutoff_cases[mode] = dict(packets=packet_count(ref), flows=len(ref_fct), end_ns=end)
result["irn_entry_controls"] = cutoff_cases

dense = {}
for count in (63, 64, 65):
    ref = root / "dense-native" / str(count)
    ref_wire = wire(ref/"wire.raw")
    ref_fct = irn_rows(ref/"reference.fct",True,None,2000100000)
    assert rc(ref) == 0 and len(ref_fct) == count
    n = f"dense-{count}-{'auto' if count == 65 else 'fixed'}"
    dense[str(count)] = dict(flows=count, wire=packet_count(ref_wire), peak=count)
    for arch in ("hip","cuda"):
        d = root / f"rdma-{arch}-final" / n
        assert rc(d) == 0 and clean_rdma((d/"run.log").read_text()), d
        inp = d.parent / "dense-inputs" / str(count) / "flows.csv"
        assert wire(d/"wire.raw") == ref_wire, d
        assert irn_rows(d/"flows.fct",False,inp,2000100000) == ref_fct, d
        assert (d/"due.txt").read_text().splitlines()[1:] == [f"0 {count} -1"], d
        if count == 65:
            neg = d.parent / "dense-65-fixed"
            assert rc(neg) == 3 and "0 65 2000001000" in (neg/"due.txt").read_text(), neg
            text = (d/"run.log").read_text()
            assert "observed=65 first_ns=2000001000" in text and "--due-cap-auto=128" in text, d
            dense[str(count)]["first_overflow_ns"] = 2000001000
            dense[str(count)]["first_overflow_host"] = 0
result["rdma_due"] = dense

ref = review / "native/dcqcn/rdma-jobs_delay_ref/k8_a2a30_cc1_d2000"
for arch in ("hip","cuda"):
    d = root / f"dcqcn-{arch}"
    assert rc(d) == 0 and clean_rdma((d/"run.log").read_text()), d
    assert sorted(rows(d/"output.fct")) == sorted(rows(ref/"output.fct")), d
    assert wire(d/"wire.raw") == wire(ref/"wire.raw"), d
result["dcqcn_regression"] = dict(flows=len(rows(ref/"output.fct")), wire=line_count(ref/"wire.raw"))

expected = load(tcp/"expected.json")
prior = load(review/"cuda-tcp-v2/timing.json")
tcp_result = {}
for sack, arm, label in ((0,"timestamps","ts"), (1,"both","both")):
    ref = {r["signature"]["sha256"] for r in prior if r.get("arm") == arm and r["rc"] == 0 and r.get("signature")}
    assert len(ref) == 1, ref
    ref = ref.pop()
    d = root / "tcp-residency" / str(sack)
    assert fct_signature(d/"flows.csv") == ref and len(tcp_rows(d/"flows.csv")) == 10240, d
    assert clean_tcp((d/"run.log").read_text()), d
    e = root / "tcp-residency-tail" / str(sack)
    assert fct_signature(e/"flows.csv") == ref and clean_tcp((e/"run.log").read_text()), e
    whole = load(d/"residency.json")
    tails = load(e/"residency.json")
    for k in ("writes", "wide_writes", "peak_inflight", "peak_wide_inflight", "peak_wide_writes_per_arrival_slot"):
        assert whole[k] == tails[k], e
    runs = profiles(root / f"tcp-profile-{label}", lambda d, log:tcp_profile(d,log,ref))
    row = dict(residency=tails, residency_with_roots=whole, timers_and_queues={}, runs=runs, medians=totals(runs))
    for cc in ("dctcp","cubic"):
        e = expected[f"wide/{cc}/{arm}"]
        timer_ref = None
        for arch in ("hip","cuda"):
            for layout in ("wide","split"):
                case = root / f"tcp-mixed-{arch}" / f"{cc}-{sack}-{layout}"
                assert rc(case) == 0 and clean_tcp((case/"run.log").read_text()), case
                assert sha(case/"queue.txt") == e["queue"], case
                assert fct_signature(case/"flows.csv") == e["flows"]["sha256"], case
                timers = timer_order(case/"run.log")
                assert timers and (timer_ref is None or timers == timer_ref), case
                timer_ref = timers
        row["timers_and_queues"][cc] = dict(timers=packet_count(timer_ref),
            active_callbacks=sum(fields[-1] for seq in timer_ref.values() for fields in seq),
            queue=line_count(case/"queue.txt"), flows=e["flows"]["count"], queue_sha256=e["queue"])
    tcp_result[arm] = row
tail = root / "tcp-tail-retry"
retry = load(tail/"receipt.json")
assert [r["rc"] for r in retry["attempts"]] == [3, 0]
assert "overflow 0" in (tail/"8192/run.log").read_text()
assert clean_tcp((tail/"8192/run.log").read_text())
assert fct_signature(tail/"8192/flows.csv") == tcp_result["both"]["runs"][0]["fct_sha256"]
tcp_result["forced_retry"] = retry
result["tcp_residency"] = tcp_result

runs = profiles(root/"strong-formal",strong)
result["strong"] = dict(runs=runs, medians=totals(runs))
(root/"acceptance.json").write_text(json.dumps(result, indent=2) + "\n")
print("Verified CUBIC, IRN drain, dense RDMA/DCQCN, TCP residency and split storage, and all nine strong-incast runs.")
