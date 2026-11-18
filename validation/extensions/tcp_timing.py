"""Whole-process DCTCP timing on the 128-host heterogeneous validation graph."""
import argparse
import csv
import json
import subprocess
import time
from pathlib import Path


def read_rows(path):
    with path.open() as stream:
        return [row[:6] for row in csv.reader(stream)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("twins", "reference", "topology", "out"):
        parser.add_argument(key, type=Path)
    a = parser.parse_args()
    a.out.mkdir(parents=True, exist_ok=False)
    inputs = a.out / "flows.csv"
    inputs.write_text("src,dst,flow_size,start_ns\n" + "".join(
        f"{i},{(i+67)%128},{1048576+131*i},{1000+17*(i%5)}\n" for i in range(128)))
    common = [f"--topofile={a.topology}", f"--flows={inputs}", "--ecnK=30"]
    base = {"ref": [a.reference, "--topo=file", "--tcp=ns3::TcpDctcp", "--hostq=red",
                    "--flowEcmp=1", "--stop=0.1"],
            "gpu": [a.twins, "--topo=graph", "--tcp=dctcp", "--spray=0"]}
    trials = []
    for i in range(4):
        for kind in (("ref", "gpu") if i % 2 == 0 else ("gpu", "ref")):
            cmd = [*base[kind], *common, f"--out={a.out}/{kind}-{i}.csv"]
            with (a.out / f"{kind}-{i}.log").open("w") as log:
                start = time.perf_counter()
                ret = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
                wall = time.perf_counter() - start
            trials.append(dict(program=kind, trial=i, wall=wall, rc=ret.returncode,
                               command=list(map(str, cmd))))
            (a.out / "trials.json").write_text(json.dumps(trials, indent=2))
            ret.check_returncode()
        expected = read_rows(a.out / f"ref-{i}.csv")
        if (len(expected) != 129 or expected != read_rows(a.out / f"gpu-{i}.csv")
                or any(int(row[5]) < 0 for row in expected[1:])):
            raise SystemExit(f"flow mismatch on trial {i}")


if __name__ == "__main__":
    main()
