"""Retain and compare the targeted TCP follow-up evidence on the executing GPU."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "paper_tcp_options"))
from campaign import clean, gpu_args, invoke, signature
from native import digest


def verify(project, output, phase):
    old = project / "out/tcp-options"
    inputs = old / "inputs"
    build = output / "build"
    report = {}
    if phase == "dense":
        for arm, enabled in (("off", 0), ("both", 1)):
            for allocation in ("automatic", "preallocated"):
                folder = output / f"hip/dense-final/{arm}/{allocation}"
                args = ["--k=4", f"--flows={inputs / 'capacity.csv'}", "--tcp=newreno", "--queue=1024",
                        f"--tcp-sack={enabled}", f"--tcp-timestamps={enabled}", "--out=flows.csv"]
                if allocation == "automatic":
                    cmd = [sys.executable, project / "src/tcp_retry.py", build / "twins", *args]
                else:
                    cmd = [build / ("twins-options" if enabled else "twins"), "--single-pass", "--size-active", *args]
                row = invoke(list(map(str, cmd)), folder)
                expected = signature(output / f"native/dense/{arm}/flows.csv")
                found = signature(folder / "flows.csv") if (folder / "flows.csv").is_file() else None
                row.update(expected=expected, found=found, clean=clean(folder))
                row["accepted"] = row["rc"] == 0 and row["clean"] and found == expected
                report[f"{arm}/{allocation}"] = row
    elif phase == "rto":
        folder = output / "hip/rto"
        case = "packet-ecmp", "newreno", inputs / "packet.csv", None, 200, True, False
        cmd = [str(build / "twins"), "--single-pass", *gpu_args(case, "off"), "--out=flows.csv", "--qtrace=queue.txt",
               "--qtrace_until=300000000000"]
        row = invoke(cmd, folder)
        expected = json.loads((old / "references.json").read_text())["packet-ecmp/off"]
        found = dict(flows=signature(folder / "flows.csv"), queue=digest(folder / "queue.txt"))
        row.update(expected=expected, found=found, clean=clean(folder))
        row["accepted"] = row["rc"] == 0 and row["clean"] and found == expected
        report["rto/trajectory"] = row
    else:
        expected = json.loads((old / "references.json").read_text())
        for cc in ("dctcp", "cubic"):
            for arm in ("timestamps", "both"):
                folder = output / f"hip/wide-v2/{cc}/{arm}"
                case = cc, cc, inputs / "128.csv", inputs / "128.graph", 200, False, False
                cmd = [str(build / "twins2048-options-links"), "--single-pass", *gpu_args(case, arm),
                       "--out=flows.csv", "--qtrace=queue.txt", "--qtrace_until=300000000000"]
                row = invoke(cmd, folder)
                found = dict(flows=signature(folder / "flows.csv") if (folder / "flows.csv").is_file() else None,
                             queue=digest(folder / "queue.txt") if (folder / "queue.txt").is_file() else None)
                row.update(expected=expected[f"{cc}/{arm}"], found=found, clean=clean(folder))
                row["accepted"] = row["rc"] == 0 and row["clean"] and row["expected"] == found
                report[f"{cc}/{arm}"] = row
                if row["rc"]:
                    break  # preserve the first GPU fault without repeatedly resetting the desktop GPU
            if row["rc"]:
                break
    (output / f"{phase}-acceptance.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: row["accepted"] for key, row in report.items()}), flush=True)
    return all(row["accepted"] for row in report.values())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("dense", "rto", "wide"))
    parser.add_argument("output", type=lambda value: Path(value).resolve())
    args = parser.parse_args()
    raise SystemExit(0 if verify(Path(__file__).resolve().parents[2], args.output, args.phase) else 1)
