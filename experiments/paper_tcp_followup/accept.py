"""Cross-backend acceptance for dense TCP, optimized histories and native IPv4 timeouts."""
import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "paper_tcp_options"))
from campaign import clean, gpu_args, invoke, matrix, signature
from native import digest


def run(binary, inputs, expected, output):
    output.mkdir(parents=True, exist_ok=False)
    rows = {}

    def trial(key, command, gold, queue=False):
        folder = output / key
        row = invoke(list(map(str, [*command, "--out=flows.csv", *( ["--qtrace=queue.txt"] if queue else [])])), folder)
        found = dict(flows=signature(folder / "flows.csv") if (folder / "flows.csv").is_file() else None)
        if queue: found["queue"] = digest(folder / "queue.txt") if (folder / "queue.txt").is_file() else None
        row.update(expected=gold, found=found, clean=clean(folder))
        row["accepted"] = row["rc"] == 0 and row["clean"] and all(found.get(k) == v for k,v in gold.items())
        rows[key] = row
        print(key, row["accepted"], flush=True)
        (output / "acceptance.json").write_text(json.dumps(rows,indent=2)+"\n")
        if row["rc"] < 0: raise RuntimeError(f"GPU fault in {key}, stop at the first fault")
        return row

    for arm, enabled in (("off",0), ("both",1)):
        for allocation in ("automatic", "preallocated"):
            command = [binary / "twins"]
            if allocation == "preallocated": command += ["--single-pass", "--size-active"]
            if enabled and allocation == "preallocated": command[0] = binary / "twins-options"
            command += ["--k=4", f"--flows={inputs / 'capacity.csv'}", "--tcp=newreno", "--queue=1024",
                        f"--tcp-sack={enabled}", f"--tcp-timestamps={enabled}"]
            trial(f"dense/{arm}/{allocation}", command, expected[f"dense/{arm}"])
    for cc in ("dctcp", "cubic"):
        for arm in ("timestamps", "both"):
            case = cc, cc, inputs / "128.csv", inputs / "128.graph",200,False,False
            trial(f"wide/{cc}/{arm}", [binary / "twins2048-options-links", "--single-pass", *gpu_args(case,arm),
                  "--qtrace_until=300000000000"], expected[f"wide/{cc}/{arm}"], True)
    for name, args in (("before",["-1"]),("equal-first",["0"]),("equal-last",["0","late"]),
                       ("after",["1"]),("missing",["0","missing"]),("stale",["0","stale"])):
        folder = output / f"fragments/{name}"
        row = invoke([str(binary / "fragment-probe"), *args], folder)
        text = (folder / "run.log").read_text().splitlines()
        events = [line for line in text if not line.startswith("FLAGS ")]
        flags = [line for line in text if line.startswith("FLAGS ")]
        row["accepted"] = row["rc"] == 0 and events == expected[f"probe/{name}"] and flags == ["FLAGS 0 0 0 0"]
        rows[f"fragments/{name}"] = row
        print("probe", name, row["accepted"], flush=True)
    for kind, flow, horizon in (("orphan","one.csv",62000000000000),("connections","forty.csv",72000000000000)):
        for arm in ("timestamps","both"):
            case = kind, "newreno", inputs / flow, inputs / "two-paths.graph",200,False,False
            command = [binary / "twins", *gpu_args(case,arm), f"--max_slots={(horizon+749999)//750000}",
                       f"--qtrace_until={horizon}", "--drain"]
            command += ["--drop-first-fragment-tail"] if kind == "orphan" else ["--fragment-timers=1"]
            trial(f"long/{kind}/{arm}", command, expected[f"long/{kind}/{arm}"], True)
    (output / "acceptance.json").write_text(json.dumps(rows,indent=2)+"\n")
    return all(row["accepted"] for row in rows.values())


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("binary", "inputs", "expected", "output"):
        p.add_argument(name,type=lambda v:Path(v).resolve())
    a=p.parse_args()
    raise SystemExit(0 if run(a.binary,a.inputs,json.loads(a.expected.read_text()),a.output) else 1)
