"""Prepare and run the archived 128-GPU AllToAll on a native or GPU engine."""
import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("case", type=Path)
    parser.add_argument("side", choices=("freeze","gpu","reference"))
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--topology", type=Path)
    parser.add_argument("--workload", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--wire-until", type=int)
    args = parser.parse_args()
    case = args.case.resolve()
    if args.side == "freeze":
        case.mkdir(parents=True)
        fields = args.topology.read_text().splitlines()
        n, _, nvs, sw, links, _ = fields[0].split()
        # The author reader accepts integer nanoseconds. SimAI expresses these
        # identical delays as decimal milliseconds in the archived topology.
        converted = []
        for line in fields[2:]:
            if not line.strip():
                continue
            src, dst, rate, delay, error = line.split()
            suffix = next(unit for unit in ("ms","us","ns") if delay.endswith(unit))
            factor = {"ms":10**6,"us":10**3,"ns":1}[suffix]
            ns = Decimal(delay[:-2])*factor
            if ns != int(ns):
                raise ValueError(f"author topology requires integer ns: {delay}")
            converted.append(f"{src} {dst} {rate} {int(ns)}ns {error}")
        if len(converted) != int(links):
            raise ValueError("link count differs")
        (case/"fabric.txt").write_text(f"{n} {int(nvs)+int(sw)} {links}\n"+
                                       fields[1]+"\n"+"\n".join(converted)+"\n")
        shutil.copyfile(args.topology, case/"simai.topo")
        shutil.copyfile(args.workload, case/"workload.txt")
        shutil.copyfile(args.config, case/"simai.conf")
        # Preserve experiment parameters, translating only input and trace paths.
        config = {}
        for line in args.config.read_text().splitlines():
            if line.strip():
                k,v=line.split(maxsplit=1)
                config[k]=v
        config.update(ENABLE_IRN="1", ENABLE_PFC="0", BUFFER_SIZE="1", ACK_HIGH_PRIO="1",
                      LB_MODE="0", GLOBAL_T="1", TOPOLOGY_FILE="fabric.txt", FLOW_FILE="ref.flows",
                      FLOWGEN_START_TIME="2.0", FLOWGEN_STOP_TIME="2.002")
        # NVLink cannot carry any of these inter-server messages through a
        # non-forwarding host. The author still validates every link's ECN map.
        for name,value in (("KMIN_MAP","600"),("KMAX_MAP","2400"),("PMAX_MAP","0.2")):
            count, entries = config[name].split(maxsplit=1)
            config[name] = f"{int(count)+1} {entries} 2880000000000 {value}"
        for name in list(config):
            if name.endswith("_FILE") and name not in ("TOPOLOGY_FILE","FLOW_FILE"):
                config[name]=name.lower()+".txt"
        (case/"config").write_text("".join(f"{k} {v}\n" for k,v in config.items()))
        (case/"ref.flows").write_text("0\n")
        return
    env = dict(os.environ)
    if args.side == "reference":
        env.update(AI_TOPO="simai.topo", AI_WORKLOAD="workload.txt", IRN_BDP_BYTES="127000")
        command = [str(args.binary.resolve()), "config"]
        if args.wire_until is not None:
            env.update(TWINS_TXTRACE="reference.tx", TWINS_TXTRACE_MAX_NS=str(args.wire_until))
    else:
        command = [str(args.binary.resolve()), "simai.conf", "--topo=simai.topo",
                   "--astra=workload.txt", "--out=gpu.fct", "--dag-out=gpu.dag",
                   "CC_MODE=1", "ENABLE_IRN=1", "ENABLE_PFC=0", "BUFFER_SIZE=1",
                   "ACK_HIGH_PRIO=1", "--fp-bases=65537,1048573"]
        if args.wire_until is not None:
            command += ["--txtrace=gpu.tx", f"--until={args.wire_until}"]
    start=time.monotonic()
    with (case/(args.side+".log")).open("w") as log:
        try:
            rc=subprocess.run(command, cwd=case, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=600).returncode
        except subprocess.TimeoutExpired:
            rc=124
    (case/(args.side+".rc")).write_text(f"{rc}\n")
    (case/(args.side+"-run.json")).write_text(json.dumps(dict(command=command, rc=rc, wall_s=time.monotonic()-start),indent=2))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
