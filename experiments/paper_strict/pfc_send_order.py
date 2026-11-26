"""Recheck same-port PFC transmission order against frozen native SimAI traces."""

import argparse
from collections import Counter, defaultdict
import fcntl
import gzip
from hashlib import file_digest
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from gpu_counters import patch


CASES = {"sustained-candidate": 10393, "intermittent": 212638}
PORTS = {(node, port) for node in (144, 145) for port in range(65, 73)}
TRACE_UNTIL = 400000
SOURCE_FILES = ("rdma.h", "rmain.cu", "rstep.cuh")
BACKENDS = {
    "CUDA": ("120", "/usr/local/cuda/bin/nvcc"),
    "HIP": ("gfx1200", "/opt/rocm/llvm/bin/clang++"),
}


def sha256(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def save(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def reference(raw, expected, output):
    if sha256(raw) != expected:
        raise ValueError(f"Frozen reference digest changed: {raw}")
    count = 0
    with raw.open() as source, gzip.open(output, "wt") as target:
        for index, line in enumerate(source):
            ts, node, port, *_ = line.split()
            if (int(node), int(port)) in PORTS and int(ts) <= TRACE_UNTIL:
                target.write(f"{index} {line}")
                count += 1
    if not count:
        raise ValueError(f"No selected transmissions in {raw}")


def build(source, work, backend, expected_src):
    tree = work / "source"
    tree.mkdir()
    shutil.copytree(source / "src", tree / "src")
    shutil.copy2(source / "CMakeLists.txt", tree)
    (tree / "out").mkdir()
    (tree / "out/astra").symlink_to((source / "out/astra").resolve(), target_is_directory=True)
    rdma = tree / "src/rdma"
    if {name: sha256(rdma / name) for name in SOURCE_FILES} != expected_src:
        raise ValueError("The RDMA source does not match the frozen candidate")

    kernel = rdma / "rstep.cuh"
    driver = rdma / "rmain.cu"
    patch(kernel, "    TxRec &r = s.tx[i];", "    TxRec &r = s.tx[i];\n    r.index = i;")
    patch(kernel, "        r.index = i;\n", "")
    patch(driver, """        std::sort(r.begin(), r.end(), [](const TxRec &a, const TxRec &b) {
            return a.ts != b.ts ? a.ts < b.ts : a.node != b.node ? a.node < b.node : a.dev < b.dev;
        });""", """        if (const char *capture = getenv("TWINS_TX_CAPTURE_ORDER"))
        {
            FILE *raw = fopen(capture, "w");
            if (!raw) { perror("opening transmission order capture"); return 1; }
            const char kinds[] = "DANPR";
            for (const TxRec &x : r)
                fprintf(raw, "%u %lld %d %d %c %u %u %u %u %u %u\\n", x.index,
                        (long long)x.ts, x.node, x.dev, kinds[x.kind], x.a, x.b, x.sport,
                        x.seq, x.size, x.ecn);
            if (fclose(raw)) { perror("closing transmission order capture"); return 1; }
        }
        std::sort(r.begin(), r.end(), [](const TxRec &a, const TxRec &b) {
            if (a.ts != b.ts) return a.ts < b.ts;
            if (a.node != b.node) return a.node < b.node;
            if (a.dev != b.dev) return a.dev < b.dev;
            return a.index < b.index;
        });""")
    patch(driver, "            n = s.tx_cap;", "            return 1;")
    out = work / "build"
    arch, compiler = BACKENDS[backend]
    commands = [
        ["cmake", "-S", tree, "-B", out, "-G", "Ninja", "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
         f"-DTWINS_GPU_BACKEND={backend}", f"-DCMAKE_{backend}_ARCHITECTURES={arch}",
         f"-DCMAKE_{backend}_COMPILER={compiler}"],
        ["cmake", "--build", out, "--target", "twins-ai", "-j4"],
    ]
    with (work / "build.log").open("wb") as log:
        for command in commands:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=900, check=True)
    binary = out / "twins-ai"
    save(work / "build-receipt.json", {
        "backend": backend, "commands": [[str(x) for x in cmd] for cmd in commands],
        "base_source_sha256": expected_src, "source_sha256": {n: sha256(rdma / n) for n in SOURCE_FILES},
        "binary_sha256": sha256(binary),
    })
    return binary


def read_wire(stream, ordinal):
    groups = defaultdict(list)
    count = 0
    last = -1
    last_key = None
    for count, line in enumerate(stream, 1):
        fields = line.split()
        if ordinal:
            index = int(fields.pop(0))
            if index <= last:
                raise ValueError("Transmission ordinals are not increasing")
            last = index
        if len(fields) != 10 or (int(fields[1]), int(fields[2])) not in PORTS:
            raise ValueError(f"Unexpected transmission: {line!r}")
        key = tuple(map(int, fields[:3]))
        if not ordinal and last_key is not None and key < last_key:
            raise ValueError("Exported transmission keys are not sorted")
        last_key = key
        groups[key].append(" ".join(fields[3:]))
    if not count:
        raise ValueError("Transmission trace was empty")
    if ordinal and not str(stream.name).endswith(".gz") and last != count - 1:
        raise ValueError("GPU capture lost at least one emission index")
    return groups, count


def compare(reference_groups, observed_groups):
    mismatches = [(k, reference_groups.get(k, []), observed_groups.get(k, []))
                  for k in sorted(reference_groups.keys() | observed_groups.keys())
                  if reference_groups.get(k) != observed_groups.get(k)]
    content_differences = sum(Counter(expected) != Counter(observed) for _, expected, observed in mismatches)
    return {"order_mismatches": len(mismatches), "content_mismatches": content_differences,
            "first_differences": mismatches[:8]}


def normalize(path, dag=False):
    lines = path.read_text().splitlines()
    if dag:
        lines = [re.sub(r"^(on [ft][0-9]* m).*", r"\1", line) for line in lines if not line.startswith("#")]
    return sorted(line.rstrip() for line in lines)


def measure(binary, work, inputs, core, capture_order):
    port_list = work / "selected-ports.txt"
    port_list.write_text("".join(f"{node} {port}\n" for node, port in sorted(PORTS)))
    summary = {}
    for case, ts in CASES.items():
        base = inputs / case
        with gzip.open(base / "reference-ordinal.tsv.gz", "rt") as stream:
            expected, ref_count = read_wire(stream, ordinal=True)
        witness = (ts, 144, 65)
        if [packet[0] for packet in expected.get(witness, [])] != ["P", "D"]:
            raise ValueError(f"Original witness missing for {case}")
        summary[case] = {"reference_rows": ref_count, "witness": witness, "arms": {}}
        for arm in ("safe", "serial"):
            trial = work / case / arm
            trial.mkdir(parents=True)
            env = dict(os.environ, TWINS_TRACE_PORTS=str(port_list))
            if capture_order:
                env["TWINS_TX_CAPTURE_ORDER"] = "raw-order.tsv"
            command = ["taskset", "-c", core, str(binary), str(base / "config.log"), f"--topo={base / 'topo.log'}",
                       f"--astra={base / 'workload.log'}", "--out=messages.fct", "--dag-out=actions.dag",
                       "--nvl-islands=0", "--gwin=0", "--fp-bases=1000003,1000033", "--txtrace=ordered.tsv",
                       f"--until={TRACE_UNTIL}"]
            if arm == "serial":
                command.append("--serial")
            with (trial / "process.log").open("wb") as log:
                result = subprocess.run(command, env=env, cwd=trial, stdout=log, stderr=subprocess.STDOUT,
                                        timeout=180, check=False)
            row = {"rc": result.returncode, "command": command}
            if result.returncode == 0:
                export = trial / "ordered.tsv"
                with export.open() as stream:
                    exported, count = read_wire(stream, ordinal=False)
                row.update(exported_rows=count, exported_vs_reference=compare(expected, exported),
                           witness_reference=expected[witness], witness_exported=exported.get(witness),
                           messages_equal=normalize(trial / "messages.fct") == normalize(base / "messages.fct"),
                           actions_equal=normalize(trial / "actions.dag", True) == normalize(base / "actions.dag", True),
                           exported_sha256=sha256(export))
            if result.returncode == 0 and capture_order:
                raw = trial / "raw-order.tsv"
                with raw.open() as stream:
                    actual, count = read_wire(stream, ordinal=True)
                row.update(rows=count, same_time_groups=sum(len(v) > 1 for v in actual.values()),
                           raw_vs_reference=compare(expected, actual), exported_vs_raw=compare(actual, exported),
                           witness_raw=actual.get(witness))
                with (trial / "witness.tsv").open("w") as output:
                    for name, source in (("reference", base / "reference-ordinal.tsv.gz"), ("gpu_raw", raw)):
                        opener = gzip.open if source.suffix == ".gz" else open
                        with opener(source, "rt") as stream:
                            for line in stream:
                                if tuple(map(int, line.split()[1:4])) == witness:
                                    output.write(f"{name}\t{line}")
                row["raw_sha256"] = sha256(raw)
            save(trial / "receipt.json", row)
            summary[case]["arms"][arm] = row
            save(work / "summary.json", summary)
            print(case, arm, row.get("exported_vs_reference"), row.get("raw_vs_reference"), flush=True)
    return int(any(row["rc"] or row["exported_vs_reference"]["order_mismatches"]
                   or (capture_order and row["raw_vs_reference"]["order_mismatches"])
                   or not row["messages_equal"]
                   or not row["actions_equal"] for case in summary.values() for row in case["arms"].values()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    extract = modes.add_parser("reference")
    extract.add_argument("raw", type=Path)
    extract.add_argument("expected_sha256")
    extract.add_argument("out", type=Path)
    run = modes.add_parser("run")
    run.add_argument("backend", choices=BACKENDS)
    run.add_argument("source", type=Path)
    verify = modes.add_parser("verify-export")
    verify.add_argument("binary", type=Path)
    for command in (run, verify):
        for name in ("work", "inputs", "lock"):
            command.add_argument(name, type=Path)
        command.add_argument("--core", default="0")
    args = parser.parse_args()
    if args.mode == "reference":
        return reference(args.raw, args.expected_sha256, args.out)
    work = args.work.resolve()
    work.mkdir(parents=True)
    inputs = args.inputs.resolve()
    capture_order = args.mode == "run"
    if capture_order:
        binary = build(args.source, work, args.backend, json.loads((inputs / "source-sha256.json").read_text()))
    else:
        binary = args.binary.resolve()
        save(work / "binary-sha256.json", {"binary": str(binary), "sha256": sha256(binary)})
    with args.lock.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return measure(binary, work, inputs, args.core, capture_order)


if __name__ == "__main__":
    raise SystemExit(main())
