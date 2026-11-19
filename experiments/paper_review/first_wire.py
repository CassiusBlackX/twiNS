"""Compare timestamp groups over all observed link sends, ignoring only cross-link order."""
import argparse
from collections import Counter
from itertools import groupby, zip_longest
import json
from pathlib import Path


def groups(lines, until):
    def rows():
        prior = -1
        for line in lines:
            fields = line.split()
            if len(fields) != 14:
                raise ValueError(f"expected 14 wire fields: {line!r}")
            stamp = int(fields[0])
            if stamp < prior:
                raise ValueError(f"trace time went backwards: {prior}, {stamp}")
            if until is not None and stamp > until:
                break
            prior = stamp
            yield stamp, tuple(fields[1:])
    for stamp, items in groupby(rows(), key=lambda item: item[0]):
        yield stamp, Counter(row for _, row in items)


def compare(reference, gpu, until):
    equal_records = 0
    with reference.open() as a, gpu.open() as b:
        for left, right in zip_longest(groups(a, until), groups(b, until)):
            if left == right:
                equal_records += left[1].total()
                continue
            stamp = min(group[0] for group in (left, right) if group is not None)
            expected = left[1] if left and left[0] == stamp else Counter()
            observed = right[1] if right and right[0] == stamp else Counter()
            return dict(equal_records=equal_records, first_observed_wire_difference_ns=stamp,
                        reference_only=list((expected-observed).elements()),
                        gpu_only=list((observed-expected).elements()))
    return dict(equal_records=equal_records, first_observed_wire_difference_ns=None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", type=Path)
    parser.add_argument("gpu", type=Path)
    parser.add_argument("--until", type=int)
    args = parser.parse_args()
    print(json.dumps(compare(args.reference, args.gpu, args.until), indent=2))


if __name__ == "__main__":
    main()
