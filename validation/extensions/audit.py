"""Replay the frozen A4 inventory. Run from the original data checkout."""
import argparse
import csv
import hashlib
import json
import re
import subprocess
import time
from itertools import zip_longest
from pathlib import Path


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def matches(output, reference, tcp):
    with output.open() as left, reference.open() as right:
        if tcp:
            a, b = csv.reader(left), csv.reader(right)
            next(a)
            next(b)
            return all(x is not None and y is not None and x[:6] == y[:6]
                       for x, y in zip_longest(a, b))
        return sorted(line.split() for line in left) == sorted(line.split() for line in right)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("build", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--kind", choices=("tcp", "rdma"))
    p.add_argument("--residuals", action="store_true")
    a = p.parse_args()
    build = a.build.resolve()
    a.out.mkdir(parents=True, exist_ok=False)
    manifest = Path("out/paper-evidence-20261116/reports/unified-manifest.json")
    rows = json.loads(manifest.read_text())["completed_configurations"]
    (a.out / "baseline.sha256").write_text(sha(manifest))
    for row in rows:
        config = row["config"]
        tcp = config["kind"] == "tcp"
        if a.kind and a.kind != config["kind"]:
            continue
        if a.residuals and (not tcp or row["complete_matching_unflagged"]):
            continue
        dest = a.out / config["execution_config_sha256"]
        dest.mkdir()
        program = build / ("twins" if tcp else "twins-rdma")
        cmd = [str(program)] + ([] if tcp else ["validation/hpcc/hpcc.cfg"])
        output = dest / "flows"
        cmd += config["args"] + [f"--out={output}"]
        start = time.perf_counter_ns()
        with (dest / "process.log").open("w") as log:
            result = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
        wall = time.perf_counter_ns() - start
        ref = Path(config["reference"])
        text = (dest / "process.log").read_text()
        matches_done = re.findall(r"flows (\d+) completed (\d+)" if tcp else r"flows done (\d+) of (\d+)", text)
        binaries = ([program, build / "tcp_retry.py", *(build / f"twins{n}" for n in (128, 256, 512))]
                    if tcp else [program])
        receipt = dict(config=config, command=cmd, wall_ns=wall, rc=result.returncode,
                       binary_sha256={f.name:sha(f) for f in binaries},
                       completed=bool(matches_done and matches_done[-1][0] == matches_done[-1][1]),
                       reference_verified=ref.is_file() and sha(ref) == config["reference_sha256"],
                       equal=output.exists() and ref.is_file() and matches(output, ref, tcp),
                       any_warning_or_error=bool(re.search(r"warning:|error:", text)))
        (dest / "receipt.json").write_text(json.dumps(receipt, indent=2))
        print(config["case"], {k:receipt[k] for k in ("rc","completed","reference_verified","equal","any_warning_or_error")}, flush=True)


if __name__ == "__main__":
    main()
