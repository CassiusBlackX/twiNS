"""Match completed local GPU receipts to the immutable independent IRN outputs."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "paper_scale"))
from scientific_summary import irn_case


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("prior", type=Path)
    parser.add_argument("holdout", type=Path)
    parser.add_argument("gpu_seed101", type=Path)
    parser.add_argument("gpu_seed103", type=Path)
    args = parser.parse_args()
    results = {}
    work = [
        (101, "plain", args.prior, args.gpu_seed101/"plain"),
        (101, "ecn", args.prior/"dcqcn", args.gpu_seed101/"ecn"),
        (103, "plain", args.holdout/"plain", args.gpu_seed103/"plain"),
        (103, "ecn", args.holdout/"ecn", args.gpu_seed103/"ecn"),
    ]
    for seed, kind, root, gpu in work:
        for h in (128, 256, 512, 1024):
            for mode in ("spread", "incast"):
                cpu_case = root/"cpu-scale"/mode/str(h)
                gpu_case = gpu/mode/str(h)
                key = f"{seed}/{kind}/{mode}/{h}"
                ref_done = cpu_case/"receipt.json"
                gpu_done = gpu_case/"gpu.json"
                if not ref_done.exists() or not gpu_done.exists():
                    results[key] = dict(state="pending", reference_done=ref_done.exists(), gpu_done=gpu_done.exists())
                    continue
                receipt = json.loads(gpu_done.read_text())
                if receipt["rc"] != 0:
                    results[key] = dict(state="gpu-error", receipt=receipt)
                    continue
                flows = root/"inputs"/str(h)/mode/"flows.csv"
                results[key] = irn_case(flows, cpu_case, gpu_case)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
