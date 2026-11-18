"""Verify archived A4 runs, keeping intermediate warnings and terminal outcomes."""
import argparse
import collections
import json
import re
from pathlib import Path

from audit import matches, sha


def verify(receipt):
    record = json.loads(receipt.read_text())
    cfg = record["config"]
    log = (receipt.parent / "process.log").read_text()
    tcp = cfg["kind"] == "tcp"
    counts = re.findall(r"flows (\d+) completed (\d+)" if tcp else r"flows done (\d+) of (\d+)", log)
    complete = bool(counts and counts[-1][0] == counts[-1][1])
    if tcp:
        counters = [re.findall(r"\b" + name + r" (\d+)", log) for name in
                    ("undecided-ties", "active-ovf", "ooo-ovf", "due-ovf", "rtt-ovf", "ring-ovf")]
        clean = all(values and values[-1] == "0" for values in counters)
    else:
        counters = re.findall(r"undecided ties (\d+), on lost tails (\d+) \| overflows: tx (\d+) "
                              r"queue (\d+) ackq (\d+) qps (\d+) due (\d+)", log)
        clean = bool(counters and all(v == "0" for v in counters[-1]))
    args = dict(v.split("=", 1) for v in cfg["args"] if "=" in v)
    inputs = {key: Path(args[key]) for key in cfg["input_sha256"]}
    valid = all(p.is_file() and sha(p) == cfg["input_sha256"][k] for k, p in inputs.items())
    ref, output = Path(cfg["reference"] or ""), receipt.parent / "flows"
    equal = ref.is_file() and sha(ref) == cfg["reference_sha256"] and output.is_file() and matches(output, ref, tcp)
    return dict(case=cfg["case"], kind=cfg["kind"], id=cfg["execution_config_sha256"],
                complete=complete, terminal_clean=bool(clean), inputs_verified=valid, reference_equal=equal,
                any_warning="warning:" in log, wall_ns=record["wall_ns"],
                accepted=bool(record["rc"] == 0 and complete and clean and equal and valid))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("manifest", "tcp", "rdma", "out"):
        p.add_argument(key, type=Path)
    a = p.parse_args()
    baseline = json.loads(a.manifest.read_text())["completed_configurations"]
    rows = []
    for directory in (a.tcp, a.rdma):
        if (directory / "baseline.sha256").read_text().strip() != sha(a.manifest):
            raise SystemExit("different inventory: " + str(directory))
        rows.extend(verify(f) for f in sorted(directory.glob("*/receipt.json")))
    expected = {r["config"]["execution_config_sha256"] for r in baseline}
    if len(rows) != len(expected) or {r["id"] for r in rows} != expected:
        raise SystemExit("missing or duplicate receipts")
    summary = collections.Counter((r["kind"], r["accepted"]) for r in rows)
    with a.out.open("x") as out:
        json.dump(dict(counts={str(k): v for k, v in summary.items()}, runs=rows), out, indent=2)
    print(summary)


if __name__ == "__main__":
    main()
