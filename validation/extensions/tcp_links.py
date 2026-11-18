"""Compare all TCP output fields and each queue's ordered picosecond trace."""
import argparse
import csv
import json
import os
import subprocess
from pathlib import Path


def queues(lines, prefix):
    events = []
    for line in lines:
        fields = line.split()
        if prefix:
            if fields[:1] != ["Q"]:
                continue
            fields = fields[1:]
        events.append(tuple(fields))
    return sorted(events, key=lambda e: tuple(map(int, e[:3])))


def main():
    parser = argparse.ArgumentParser()
    for opt in ("twins", "reference", "out"):
        parser.add_argument(opt, type=Path)
    parser.add_argument("--scale", action="store_true", help="128-host fabric with four spines")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    flows = args.out / "messages.csv"
    flows.write_text("src,dst,flow_size,start_ns\n" + "".join(
        f"{i},{19-i%2},{n},{1000 + 71*i}\n" for i,n in enumerate((1,1460,20000,65536,4097,63000))))
    failures = []
    for variant in (("scale",) if args.scale else ("uniform","rate","delay","mixed","oversubscribed")):
        heavy = variant == "oversubscribed"
        flow_file = flows
        if heavy:
            flow_file = args.out / "incast.csv"
            flow_file.write_text("src,dst,flow_size,start_ns\n" + "".join(
                f"{i},{19-i%2},{25000 + i*353},1000\n" for i in range(10)))
        topo = args.out / (variant + ".txt")
        lines = ["hosts 20","switches 4"]
        edges = [(f"h{i}",f"s{i//10}",(25,100)[i%2],500000) for i in range(20)]
        edges += [(f"s{i}",f"s{j}",(100,400)[j-2],(2000000,1725401)[j-2])
                  for i in range(2) for j in (2,3)]
        if args.scale:
            lines = ["hosts 128", "switches 12"]
            edges = [(f"h{i}", f"s{i//16}", (25,100)[i%2], 500000) for i in range(128)]
            edges += [(f"s{i}", f"s{8+j}", (100,100,100,400)[j], (1000000,1500000,1725401,2000000)[j])
                      for i in range(8) for j in range(4)]
            flow_file = args.out / "scale.csv"
            flow_file.write_text("src,dst,flow_size,start_ns\n" + "".join(
                f"{i},{(i+67)%128},{8192+i*131},{1000+17*(i%5)}\n" for i in range(128)))
        for a,b,rate,delay in edges:
            lines.append(f"link {a} {b} {(rate if variant in ('rate','mixed','oversubscribed','scale') else 100)*10**9} "
                         f"{delay if variant in ('delay','mixed','oversubscribed','scale') else 1000000}")
        topo.write_text("\n".join(lines)+"\n")
        for tcp, name in (("newreno","NewReno"),("dctcp","Dctcp"),("cubic","Cubic")):
            for spray in (0,1):
                d = args.out / f"{variant}-{tcp}-{spray}"
                d.mkdir()
                common = [f"--topofile={topo}",f"--flows={flow_file}"]
                if heavy:
                    common += ["--queue=12", "--ecnK=4"]
                ref = subprocess.run([args.reference, "--topo=file", *common, f"--tcp=ns3::Tcp{name}",
                                      "--stop=0.1","--hostq=red",f"--flowEcmp={1-spray}",f"--out={d}/ref.csv"],
                                     env=dict(os.environ, TRACE_ALLQ="100000000000"),capture_output=True,text=True)
                (d / "reference.log").write_text(ref.stdout + ref.stderr)
                ref.check_returncode()
                sim = subprocess.run([args.twins, "--topo=graph", *common, f"--tcp={tcp}", f"--spray={spray}",
                                      f"--out={d}/sim.csv",f"--qtrace={d}/sim.q", "--qtrace_until=100000000000"],
                                     capture_output=True,text=True)
                (d / "process.log").write_text(sim.stdout + sim.stderr)
                sim.check_returncode()
                def rows(file):
                    with file.open() as f:
                        return [r[:6] for r in csv.reader(f)]
                actual, expected = rows(d/"sim.csv"), rows(d/"ref.csv")
                # ns-3 continues after the final completion. TwiNS stops at its slot boundary.
                last = max(int(r[5]) for r in expected[1:]) * 1000
                refq = [r for r in queues(ref.stdout.splitlines(),True) if int(r[0]) <= last]
                simq = [r for r in queues((d/"sim.q").read_text().splitlines(),False) if int(r[0]) <= last]
                result = dict(equal=actual==expected, complete=all(int(r[5])>=0 for r in actual[1:]),
                              queues_equal=bool(refq) and simq==refq, enqueues=len(refq))
                (d/"result.json").write_text(json.dumps(result))
                print(d.name,result,flush=True)
                if not all(result[k] for k in ("equal", "complete", "queues_equal")):
                    failures.append(d.name)
    if failures:
        raise SystemExit("failed comparisons: " + ", ".join(failures))


if __name__ == "__main__":
    main()
