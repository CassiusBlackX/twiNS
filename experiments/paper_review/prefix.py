"""Capture a full physical-link prefix on an unchanged frozen incast or AI workload."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("campaign", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("binary", type=Path)
    parser.add_argument("side", choices=("gpu", "reference"))
    parser.add_argument("case", choices=("ecn128", "plain256", "ai-small", "ai-large"))
    parser.add_argument("--until", type=int)
    parser.add_argument("--last-start", type=int)
    parser.add_argument("--ai-inputs", type=Path)
    parser.add_argument("--no-wire", action="store_true",
                        help="measure the full process without per-packet tracing")
    args, options = parser.parse_known_args()
    supported = ("--serial", "--tail=", "--persist=", "--gwin=", "--rank=", "--win=")
    if any(opt != "--serial" and not opt.startswith(supported[1:]) for opt in options):
        parser.error(f"unsupported GPU diagnostic arguments {options!r}")
    if args.side != "gpu" and options:
        parser.error("GPU diagnostic options require side=gpu")
    campaign, output, binary = (p.resolve() for p in (args.campaign, args.out, args.binary))
    output.mkdir(parents=True)
    env = dict(os.environ)
    env.pop("TWINS_TRACE_PORTS", None)
    env.pop("TWINS_SAMPLE_FIRST_SLOT", None)
    command = [str(binary), "config"]
    inputs = []
    if args.case.startswith("ai-"):
        if args.last_start is not None:
            parser.error("--last-start applies only to frozen incast inputs")
        ai = args.ai_inputs.resolve() if args.ai_inputs else campaign / "ai-cap"
        source = ai / ("small-buffer" if args.case == "ai-small" else "large-buffer")
        for filename in ("fabric.txt", "simai.topo"):
            path = ai / filename
            inputs.append(path)
            shutil.copyfile(path, output / filename)
        for filename in ("config", "ref.flows", "workload.txt"):
            path = source / filename
            inputs.append(path)
            shutil.copyfile(path, output / filename)
        config = (source / "config").read_text().replace("../fabric.txt", "fabric.txt")
        env.update(AI_TOPO="simai.topo", AI_WORKLOAD="workload.txt", IRN_BDP_BYTES="104000")
        if args.side == "gpu":
            command += ["--topo=simai.topo", "--astra=workload.txt", "--dag-out=gpu.dag",
                        "--q3cap=65536", "IRN_BDP=104000"]
    else:
        source = campaign / ("dcqcn/inputs/128" if args.case == "ecn128" else "inputs/256")
        config_path = source / "incast/config"
        flows = source / "incast/ref.flows"
        fabric = source / "fabric.txt"
        gpu_flows = source / "incast/flows.csv"
        inputs.extend((config_path, flows, fabric, gpu_flows, source / "uniform.graph"))
        if args.last_start is not None:
            # Preserve order and topology. The author file expresses time in seconds.
            from decimal import Decimal
            rows = flows.read_text().splitlines()[1:]
            kept = [row for row in rows if Decimal(row.split()[-1])*10**9 <= args.last_start]
            flows = output / "ref.flows"
            flows.write_text(str(len(kept)) + "\n" + "\n".join(kept) + "\n")
            with gpu_flows.open(newline="") as stream:
                reader = csv.DictReader(stream)
                fields = reader.fieldnames
                reduced = [row for row in reader if int(row["start_ns"]) <= args.last_start]
            if not kept or len(kept) != len(reduced):
                raise ValueError("reference and GPU prefix sizes disagree")
            gpu_flows = output / "flows.csv"
            with gpu_flows.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(reduced)
            inputs.extend((flows, gpu_flows))
        config = config_path.read_text().replace("../fabric.txt", str(fabric)).replace("FLOW_FILE ref.flows", f"FLOW_FILE {flows}")
        env["IRN_BDP_BYTES"] = "156000"
        if args.side == "gpu":
            command += [f"--topo={source}/uniform.graph", f"--flows={gpu_flows}", "IRN_BDP=156000"]
    (output / "config").write_text(config)
    if args.side == "reference" and not args.no_wire:
        env["TWINS_TXTRACE"] = "reference.tx"
    elif args.side == "gpu":
        command += ["--out=gpu.fct", "--fp-bases=65537,1048573"]
        if not args.no_wire:
            command.append("--txtrace=gpu.tx")
            if args.until is not None:
                command.append(f"--until={args.until}")
        command += options
    start = time.monotonic()
    with (output / (args.side + ".log")).open("w") as log:
        try:
            rc = subprocess.run(command, cwd=output, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=1800).returncode
        except subprocess.TimeoutExpired:
            rc = 124
    if args.side == "gpu" and not args.no_wire and " records, " in (output/"gpu.log").read_text():
        # A truncated GPU trace cannot establish wire equality.
        rc = rc or 3
    manifest = {}
    for file in [*inputs, binary]:
        with file.open("rb") as stream:
            manifest[str(file)] = hashlib.file_digest(stream, "sha256").hexdigest()
    (output / "receipt.json").write_text(json.dumps(dict(rc=rc, wall_s=time.monotonic()-start,
        command=command, inputs=manifest, cap_ns=args.until, last_start_ns=args.last_start), indent=2))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
