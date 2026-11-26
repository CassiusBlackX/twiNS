"""Compare closed-loop identities, action inventories, and optional execution order."""
import argparse
from collections import Counter
from itertools import zip_longest
import json
from pathlib import Path
import re


def read_rows(path):
    with path.open() as stream:
        for line in stream:
            row = line.split()
            if row and not row[0].startswith("#"):
                yield tuple(row)


def compare_messages(reference, candidate):
    def inventory(path):
        items = {}
        duplicate = set()
        for row in read_rows(path):
            if len(row) != 3:
                raise ValueError(f"{path}: expected identity, start and completion: {row}")
            identity, start, completion = map(int, row)
            if identity in items:
                duplicate.add(identity)
            items[identity] = (start, completion)
        return items, sorted(duplicate)

    expected, duplicate_reference = inventory(reference)
    observed, duplicate_candidate = inventory(candidate)
    difference = sorted(i for i in expected.keys() & observed.keys() if expected[i] != observed[i])
    return dict(
        reference_count=len(expected), candidate_count=len(observed),
        duplicate_reference=duplicate_reference, duplicate_candidate=duplicate_candidate,
        missing=sorted(expected.keys() - observed.keys()),
        extra=sorted(observed.keys() - expected.keys()),
        different=[dict(identity=i, expected=expected[i], actual=observed[i]) for i in difference],
    )


def actions(path):
    for row in read_rows(path):
        # Sequential scheduler setup UIDs and local action ordinals differ.
        # Callback order (with normalized roots) is checked from its own log.
        if len(row) >= 3 and row[0] == "on" and re.fullmatch(r"m\d*", row[2]):
            row = (*row[:2], "m")
        yield row


def compare_actions(reference, candidate):
    expected, actual = Counter(actions(reference)), Counter(actions(candidate))
    return dict(missing=list((expected - actual).items()), extra=list((actual - expected).items()))

def setup_order(path):
    roots = [row for row in read_rows(path) if len(row) >= 3 and row[0] == "on" and row[2] == "m"]
    if any(len(row) != 4 for row in roots):
        return None
    return [row[1] for row in sorted(roots, key=lambda row: int(row[3]))]


def compare_execution(reference, candidate):
    for i, (a, b) in enumerate(zip_longest(read_rows(reference), read_rows(candidate)), 1):
        if a != b:
            return dict(line=i, expected=a, actual=b)
    return None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("reference", "candidate", "reference_actions", "candidate_actions"):
        p.add_argument(key, type=Path)
    p.add_argument("--reference-execution", type=Path)
    p.add_argument("--candidate-execution", type=Path)
    opt = p.parse_args()
    if bool(opt.reference_execution) != bool(opt.candidate_execution):
        p.error("both execution-order logs are required to compare order")
    messages = compare_messages(opt.reference, opt.candidate)
    inventory = compare_actions(opt.reference_actions, opt.candidate_actions)
    ref_roots = setup_order(opt.reference_actions)
    gpu_roots = setup_order(opt.candidate_actions)
    root_checked = ref_roots is not None and gpu_roots is not None
    root_difference = root_checked and ref_roots != gpu_roots
    order = compare_execution(opt.reference_execution, opt.candidate_execution) if opt.reference_execution else None
    matches = not (any(messages[key] for key in ("duplicate_reference", "duplicate_candidate",
                  "missing", "extra", "different")) or any(inventory.values()) or root_difference or order is not None)
    print(json.dumps(dict(matched=matches, messages=messages, actions=inventory,
                          setup_order_checked=root_checked, setup_order_differs=root_difference,
                          callback_order_checked=bool(opt.reference_execution), first_callback_difference=order)))
    return 0 if matches else 1


if __name__ == "__main__":
    raise SystemExit(main())
