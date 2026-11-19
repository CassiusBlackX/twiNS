"""Check whether two ASTRA DAGs differ after identifying timers by their parent and ordinal."""
import argparse
from collections import Counter
from functools import cache
import json
from pathlib import Path


def dag(path, *, normalize_main_order=False):
    timers={}
    causes={}
    flow_rows=[]
    main_order=[]
    for line in path.read_text().splitlines():
        row=line.split()
        if not row or row[0].startswith("#"):
            continue
        if row[0]=="timer":
            key="t"+row[1]
            if key in timers:
                raise ValueError(f"repeated timer declaration {key}")
            timers[key]=int(row[2])
        elif row[0]=="flow":
            flow_rows.append(tuple(row[1:]))
        elif row[0]=="on":
            child,parent=row[1:3]
            ordinal=int(row[3]) if len(row)>3 else None
            causes.setdefault(child,[]).append((parent,ordinal))
            if normalize_main_order and parent=="m":
                main_order.append((len(main_order) if ordinal is None else ordinal,child))
        else:
            raise ValueError(f"unexpected record {row}")

    # Historical SimAI records main's ns-3 UIDs while the live frontend
    # records main's scheduling order. Use that order for both recordings.
    for index, (_, child) in enumerate(sorted(main_order)):
        causes[child] = [(parent, index if parent=="m" else ordinal)
                         for parent,ordinal in causes[child]]

    @cache
    def timer(key):
        links=causes[key]
        if len(links)!=1:
            raise ValueError(f"timer {key} does not have one creation")
        parent,ordinal=links[0]
        if parent=="m":
            # Main inserts timers in its own observed setup order.
            return ("timer","m",int(key[1:]) if ordinal is None else ordinal,timers[key])
        return ("timer",owner(parent),ordinal,timers[key])

    def owner(key):
        return timer(key) if key.startswith("t") else key

    identities={key:timer(key) for key in timers}
    if len(set(identities.values()))!=len(identities):
        raise ValueError("timer identities collide, raw multiplicity and ordering require additional context")
    counts=Counter(("flow",*row) for row in flow_rows)
    counts.update(identities.values())
    counts.update(("on",owner(child),owner(parent),ord)
                  for child,links in causes.items() for parent,ord in links)
    return counts


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("reference",type=Path)
    parser.add_argument("candidate",type=Path)
    a=parser.parse_args()
    left,right=dag(a.reference),dag(a.candidate)
    print(json.dumps(dict(equal=left==right,reference=left.total(),gpu=right.total(),
                          matches=(left&right).total(),reference_only=list((left-right).items())[:4],
                          gpu_only=list((right-left).items())[:4]),indent=2))
