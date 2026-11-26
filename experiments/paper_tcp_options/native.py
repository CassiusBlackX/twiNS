"""Build the scenario against existing optimized Unison libraries and run four TCP arms."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import time


ARMS = {"off": (0, 0), "sack": (1, 0), "timestamps": (0, 1), "both": (1, 1)}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build(tree, project, output):
    cache = tree / "cmake-cache"
    source = project / "validation/ns3/twins-ref.cc"
    original = tree / "scratch/twins-ref.cc"
    target = tree / "build/scratch/ns3.46.1-twins-ref-optimized"
    entries = json.loads((cache / "compile_commands.json").read_text())
    entry = next(row for row in entries if Path(row["file"]) == original)
    compile_args = shlex.split(entry["command"])
    obj = output / "reference.o"
    compile_args[compile_args.index("-o") + 1] = str(obj)
    compile_args[-1] = str(source)
    compile_args[1:1] = ["-I" + str(project / "src"), "-ffp-contract=off"]
    commands = subprocess.check_output(["ninja", "-C", cache, "-t", "commands", target], text=True)
    link_args = [str(obj) if token == "scratch/CMakeFiles/scratch_twins-ref.dir/twins-ref.cc.o" else token
                 for token in shlex.split(commands.splitlines()[-1]) if token not in (":", "&&")]
    binary = output / "reference"
    link_args[link_args.index("-o") + 1] = str(binary)
    output.mkdir(parents=True, exist_ok=True)
    (output / "build.json").write_text(json.dumps(dict(source_sha256=digest(source), compile=compile_args,
                                                       link=link_args), indent=2) + "\n")
    with (output / "build.log").open("wb") as log:
        subprocess.run(compile_args, cwd=entry["directory"], stdout=log, stderr=subprocess.STDOUT, check=True)
        subprocess.run(link_args, cwd=cache, stdout=log, stderr=subprocess.STDOUT, check=True)


def run_arm(binary, output, arm, scenario):
    output.mkdir(parents=True)
    sack, ts = ARMS[arm]
    command = [str(binary), "--threads=0", f"--tcp-sack={sack}", f"--tcp-timestamps={ts}",
               "--out=flows.csv", *scenario]
    started = time.monotonic_ns()
    with (output / "run.log").open("wb") as log:
        try:
            rc = subprocess.run(command, cwd=output, stdout=log, stderr=subprocess.STDOUT,
                                timeout=900).returncode
        except subprocess.TimeoutExpired:
            rc = 124
    receipt = dict(command=command, rc=rc, wall_ns=time.monotonic_ns() - started,
                   binary_sha256=digest(binary))
    for key in ("TRACE_ALLQ", "TRACE_ALLQ_FROM"):
        if key in os.environ:
            receipt[key] = os.environ[key]
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return rc


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    compiler = sub.add_parser("build")
    for name in ("tree", "project", "output"):
        compiler.add_argument(name, type=Path)
    runner = sub.add_parser("run")
    runner.add_argument("binary", type=Path)
    runner.add_argument("output", type=Path)
    runner.add_argument("arm", choices=ARMS)
    runner.add_argument("scenario", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == "build":
        build(args.tree.resolve(), args.project.resolve(), args.output.resolve())
    else:
        raise SystemExit(run_arm(args.binary.resolve(), args.output.resolve(), args.arm, args.scenario))
