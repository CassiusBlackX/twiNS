"""Compare the returned CUDA runs to independent references held locally."""
import argparse
import json
from pathlib import Path
import sys

from causal_dag import dag
from first_wire import compare as compare_wire

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"paper_scale"))
from ai_compare import compare as compare_ai
from scientific_summary import irn_case


def run(prior, review, cuda):
    results = {}

    def record(key, checker, *args):
        try:
            results[key] = checker(*args)
        except (OSError, ValueError, AttributeError, KeyError) as exc:
            results[key] = {"accepted": False, "error": repr(exc)}

    def check_irn(flows, reference, gpu):
        if json.loads((gpu/"gpu.json").read_text())["rc"]:
            raise ValueError(f"GPU process failed in {gpu}")
        return irn_case(flows, reference, gpu)

    def check_wire(reference, gpu, until=None):
        row = compare_wire(reference, gpu, until)
        row["accepted"] = row["equal_records"] > 0 and row["first_observed_wire_difference_ns"] is None
        return row

    def check_ai(gpu, reference):
        if (gpu/"receipt.json").exists():
            receipt = json.loads((gpu/"receipt.json").read_text())
            (gpu/"gpu.rc").write_text(f"{receipt['rc']}\n")
        row = compare_ai(gpu, reference)
        row["same_causal_dag"] = dag(reference/"reference.dag") == dag(gpu/"gpu.dag")
        row["accepted"] &= row["same_causal_dag"]
        return row

    for seed, kind, inputs, reference in (
        (101, "plain", prior/"inputs", prior/"a87"/"cpu-scale"),
        (101, "ecn", prior/"dcqcn"/"inputs", prior/"dcqcn"/"cpu-scale"),
        *((103, kind, review/"irn-holdout"/kind/"inputs", review/"a87-final"/"holdout"/kind/"cpu-scale")
          for kind in ("plain", "ecn")),
    ):
        for hosts in (128, 256, 512, 1024):
            for mode in ("spread", "incast"):
                name = f"{mode}/{hosts}"
                record(f"irn/{seed}/{kind}/{name}", check_irn, inputs/str(hosts)/mode/"flows.csv",
                       reference/name, cuda/"matrix"/str(seed)/kind/name)

    anchors = prior.parents[2]/"twins-extensions"/"out"/"irn-wire-final"
    for name in ("one", "cross", "ecn", "moderate", "incast", "scale"):
        record(f"anchor/{name}", check_wire, anchors/name/"reference.tx", cuda/"anchors"/name/"gpu.tx")
    for mode in ("serial", "parallel"):
        for fixture in ("irn-incast-256", "irn-spread-1024", "irn-incast-1024"):
            reference = prior/"dcqcn"/"reference-traces"/fixture/"reference.tx"
            record(f"wire/{mode}/{fixture}", check_wire, reference, cuda/"wire"/mode/fixture/"gpu.tx")

    for name in ("small", "large"):
        reference = prior/"a87"/"ai-wire"/f"{name}-buffer"
        record(f"ai/{name}", check_ai, cuda/f"ai-{name}", reference)
    reference = review/"a87-final"/"ai128-final"
    record("ai/128", check_ai, cuda/"ai128", reference)
    record("ai/128-wire", check_wire, reference/"reference.tx", cuda/"ai128"/"gpu.tx", 100000)
    return {"accepted": all(row["accepted"] for row in results.values()), "cases": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("prior", "review", "cuda"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.prior.resolve(), args.review.resolve(), args.cuda.resolve()), indent=2))
