"""Generate independent IRN permutations without modifying the archived primary campaign."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"paper_scale"))
from freeze import generate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("prior", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--seed", type=int, default=103)
    args = parser.parse_args()
    for name, original in (("plain", args.prior/"inputs"), ("ecn", args.prior/"dcqcn/inputs")):
        base = args.out / name / "inputs"
        base.mkdir(parents=True)
        sizes = []
        for hosts in (128, 256, 512, 1024):
            original_case = original / str(hosts)
            sizes.append(generate(base, hosts, original_case/"incast/config", irn_seed=args.seed))
            for mode in ("spread", "incast"):
                shutil.copyfile(original_case/mode/"config", base/str(hosts)/mode/"config")
            for topology in ("fabric.txt", "uniform.graph", "heterogeneous.graph"):
                if (original_case/topology).read_bytes() != (base/str(hosts)/topology).read_bytes():
                    raise ValueError(f"topology differs at {hosts}: {topology}")
        files = {str(p.relative_to(base)):hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(base.rglob("*")) if p.is_file()}
        (base/"manifest.json").write_text(json.dumps(dict(sizes=sizes, sha256=files, irn_seed=args.seed,
            ecn=name=="ecn"), indent=2))


if __name__ == "__main__":
    main()
