"""Analyze threshold regret, with each arrival trajectory as the pairing unit."""
import argparse
import json
from pathlib import Path
from statistics import mean, median

THRESHOLDS = (10, 20, 30, 40)
SLO_NS = 1_000_000


def load(receipts):
    data = {}
    for path in receipts.glob("a*-o*-k*/receipt.json"):
        row = json.loads(path.read_text())
        key = row["arrival"], row["order"], row["k"]
        if key in data:
            raise ValueError(f"duplicate input {key}")
        if row["p99_ns"] <= 0:
            raise ValueError(f"invalid p99 {path}")
        data[key] = row["p99_ns"]
    if set(data) != {(a, o, k) for a in range(1, 21) for o in range(6) for k in THRESHOLDS}:
        raise ValueError("incomplete ECN matrix")
    return data


def analyze(data):
    # Minimize mean short-flow p99 across the same 20 trajectories, then use smaller K for ties.
    def rank(order, choices):
        return sorted(choices, key=lambda k: (mean(data[a, order, k] for a in range(1, 21)), k))
    original = rank(0, THRESHOLDS)[0]
    orders = []
    pairs = []
    for order in range(6):
        chosen = rank(order, THRESHOLDS)[0]
        stats = {}
        for k in THRESHOLDS:
            samples = [data[a, order, k] for a in range(1, 21)]
            regret = [
                value - min(data[a, order, option] for option in THRESHOLDS)
                for a, value in enumerate(samples, 1)
            ]
            stats[k] = dict(mean_p99_ns=mean(samples), median_p99_ns=median(samples),
                            slo_violations=sum(value >= SLO_NS for value in samples),
                            mean_regret_ns=mean(regret),
                            median_relative_regret=median(r/(v-r) for r,v in zip(regret,samples)),
                            max_regret_ns=max(regret))
        orders.append(dict(order=order, ranking=rank(order, THRESHOLDS), recommendation=chosen,
                           ranking_k10_to_k30=rank(order, THRESHOLDS[:3]), settings=stats,
                           chosen_vs_original_mean_ns=stats[chosen]["mean_p99_ns"]-stats[original]["mean_p99_ns"]))
        for arrival in range(1, 21):
            p99 = {k: data[arrival, order, k] for k in THRESHOLDS}
            pairs.append(dict(arrival=arrival, order=order, p99_ns=p99,
                              delta_k40_k30_ns=p99[40]-p99[30],
                              relative_delta_k40_k30=p99[40]/p99[30]-1,
                              crosses_slo=p99[30]<SLO_NS<=p99[40]))
    return dict(criterion="minimum mean of paired arrival-level short-flow p99, tie to smaller K",
                slo_ns=SLO_NS, original_recommendation=original, orders=orders, pairs=pairs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("receipts", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze(load(args.receipts)), indent=2))
