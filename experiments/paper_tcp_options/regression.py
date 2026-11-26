"""Replay the previously accepted double-off TCP release inventory."""
import argparse
import json
from pathlib import Path

from campaign import clean, invoke, signature


def freeze(previous, path):
    inventory = []
    for receipt in sorted(previous.glob("*/result.json")):
        row = json.loads(receipt.read_text())
        if row["kind"] != "tcp" or not row["accepted"]:
            continue
        args = [arg for arg in row["command"][1:] if not arg.startswith("--out=")]
        inventory.append(dict(id=receipt.parent.name, case=row["case"], suite=row["suite"], args=args,
                              expected=row["expected"], reference_sha256=row["reference_sha256"]))
    if not inventory:
        raise ValueError("no accepted TCP scenarios in the published release receipts")
    inventory.sort(key=lambda row: (row["suite"], row["case"], row["id"]))
    path.write_text(json.dumps(inventory, indent=2) + "\n")


def run(binary, data, path, output, limit):
    inventory = json.loads(path.read_text())
    output.mkdir(parents=True, exist_ok=True)
    ran = 0
    for case in inventory:
        folder = output / case["id"]
        if (folder / "result.json").exists():
            continue
        args = []
        for arg in case["args"]:
            key, _, val = arg.partition("=")
            if key in ("--flows", "--topofile", "--ecmp_from"):
                arg = f"{key}={data / val}"
            args.append(arg)
        observation = invoke([binary, *args, "--tcp-sack=0", "--tcp-timestamps=0", "--out=flows.csv"], folder)
        flow = folder / "flows.csv"
        actual = signature(flow) if flow.is_file() else None
        accepted = bool(actual and actual["count"] == actual["completed"] and actual["sha256"] == case["expected"]
                        and observation["rc"] == 0 and clean(folder))
        evidence = dict(**case, actual=actual, accepted=accepted)
        (folder / "result.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(case["suite"], case["case"], accepted, flush=True)
        ran += 1
        if limit and ran >= limit:
            break
    results = [json.loads(p.read_text()) for p in output.glob("*/result.json")]
    result = dict(total=len(inventory), tested=len(results), accepted=sum(row["accepted"] for row in results))
    (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    return result["tested"] == result["accepted"]


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    actions = cli.add_subparsers(dest="action", required=True)
    freeze_cli = actions.add_parser("freeze")
    freeze_cli.add_argument("previous", type=Path)
    freeze_cli.add_argument("inventory", type=Path)
    run_cli = actions.add_parser("run")
    for key in ("binary", "data", "inventory", "output"):
        run_cli.add_argument(key, type=lambda value: Path(value).resolve())
    run_cli.add_argument("--limit", type=int)
    args = cli.parse_args()
    if args.action == "freeze":
        freeze(args.previous, args.inventory)
    else:
        raise SystemExit(0 if run(args.binary, args.data, args.inventory, args.output, args.limit) else 1)
