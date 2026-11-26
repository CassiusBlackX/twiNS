"""Compile the complete HIP retry family on a CPU build server, retaining commands and results."""
from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

root = Path.cwd()
output = Path(sys.argv[1]).resolve()
output.mkdir(parents=True, exist_ok=False)
tasks = []
for base, runs, link_runs in (("twins", 9, 3), ("twins128", 25, 7), ("twins256", 57, 15), ("twins512", 121, 31)):
    for options in (False, True):
        for links in (False, True):
            name = base + ("-options" if options else "") + ("-links" if links else "")
            definitions = [f"-DTWINS_CHAIN_RUNS={link_runs if links else runs}"]
            if name == "twins": definitions.append("-DTWINS_RETRY_DRIVER")
            if options: definitions.append("-DTWINS_TCP_OPTIONS")
            if links: definitions.append("-DTWINS_LINK_TIMING")
            if links or base != "twins": definitions.append("-DTWINS_TCP_TIMER_ORDER")
            if links: definitions += ["-O2", "-fno-inline-functions"]
            tasks.append((name, definitions, None))
tasks.append(("twins2048-options-links", ["-DTWINS_CHAIN_RUNS=127", "-DTWINS_TCP_OPTIONS", "-DTWINS_LINK_TIMING",
              "-DTWINS_TCP_TIMER_ORDER", "-O2", "-fno-inline-functions"], None))
tasks.append(("fragment-probe", ["-DTWINS_TCP_OPTIONS", "-DTWINS_LINK_TIMING", "-DTWINS_TCP_TIMER_ORDER",
              "-DTWINS_FRAGMENT_PROBE", "-DTWINS_TRACE_FRAGMENTS", "-O2", "-fno-inline-functions"],
              "experiments/paper_tcp_followup/fragment-gpu.cu"))

def source_hashes():
    return {str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((root / "src").rglob("*")) if path.is_file() and "__pycache__" not in path.parts}

before = source_hashes()

def one(task):
    name, flags, source = task
    command = ["bash", "experiments/paper_tcp_options/build_hip.sh", str(output / name), *flags]
    env = os.environ.copy()
    if source: env["TWINS_HIP_SOURCE"] = source
    start = time.monotonic_ns()
    with (output / (name + ".log")).open("wb") as log:
        rc = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT).returncode
    print(name, rc, flush=True)
    return dict(name=name, command=command, source=source or "src/main.cu", rc=rc, wall_ns=time.monotonic_ns()-start)

with ThreadPoolExecutor(max_workers=3) as pool:
    results = list(pool.map(one, tasks))
shutil.copy2(root / "src/tcp_retry.py", output / "tcp_retry.py")
(output / "builds.json").write_text(json.dumps(results, indent=2) + "\n")
after = source_hashes()
(output / "source-hashes.json").write_text(json.dumps(dict(before=before,after=after,unchanged=before==after),indent=2)+"\n")
if before != after: raise RuntimeError("source changed during compilation")
raise SystemExit(0 if all(row["rc"] == 0 for row in results) else 1)
