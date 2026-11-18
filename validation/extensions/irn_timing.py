"""Time whole native processes. Also runs under the pinned oracle's Python 2."""
from __future__ import print_function
import argparse
import json
import os
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("reference", "twins"))
    parser.add_argument("binary")
    parser.add_argument("fixtures")
    parser.add_argument("--trials", type=int, default=3)
    args = parser.parse_args()
    clock = getattr(time, "perf_counter", time.time)
    for case in ("one", "incast", "scale"):
        directory = os.path.join(args.fixtures, case)
        for i in range(args.trials + 1):
            name = "%s-%d" % (args.kind, i)
            command = [os.path.abspath(args.binary), "config"]
            if args.kind == "twins":
                command += ["--topo=../graph", "--flows=flows.csv", "--out=" + name + ".fct"]
            with open(os.path.join(directory, name + ".log"), "w") as log:
                start = clock()
                subprocess.check_call(command, cwd=directory, stdout=log, stderr=log)
                elapsed = clock() - start
            if args.kind == "reference":
                os.rename(os.path.join(directory, "reference.fct"), os.path.join(directory, name + ".fct"))
            print(json.dumps(dict(case=case, trial=i, seconds=elapsed)))


if __name__ == "__main__":
    main()
