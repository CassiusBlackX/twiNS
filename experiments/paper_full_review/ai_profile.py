"""Profile worker collection and isolate cold/cache effects on the same closed network."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
p=argparse.ArgumentParser(description=__doc__)
p.add_argument("binary",type=lambda value:Path(value).resolve())
p.add_argument("output",type=lambda value:Path(value).resolve())
a=p.parse_args()
a.output.mkdir(parents=True,exist_ok=False)
base=Path("/home/cassius/Videos/twins-paper-evidence-20261116")
for name,n,wl,phases in (
    ("gpt13b",128,"gpt3_13b_full_128.txt",[(16,"cold",None),(0,"warm",None),(16,"warm",None),(16,"changed-warm",80),(16,"changed-cold",80)]),
    ("gpt175b",1024,"gpt3_175b_probe_1024.txt",[(16,"cold",None),(16,"warm",None),(0,"warm",None)])):
    topo=base/"data/simai"/f"hpn{n}.topo"
    workload=base/"out/simai/wl/paper"/wl
    inputs=[hashlib.sha256(f.read_bytes()).hexdigest() for f in (topo,workload)]
    cache=a.output/name/("flows-"+"-".join(x[:12] for x in inputs)+".dag")
    for workers,label,buffer in phases:
        out=a.output/name/f"p{workers}-{label}"
        out.mkdir(parents=True)
        cmd=["taskset","-c","0-16" if workers else "0",a.binary,base/"validation/simai/SimAI.conf",
             f"--topo={topo}",f"--astra={workload}","--out=messages.fct","--dag-out=actions.dag",
             "--fp-bases=1000003,1000033",f"--nvl-islands={workers}"]
        selected=out/"rebuild.dag" if label=="changed-cold" else cache
        cmd.append(f"--flows-cache={selected}")
        if buffer is not None: cmd.append(f"BUFFER_SIZE={buffer}")
        env={**os.environ,"TWINS_STUB_PROFILE":"1"}
        if workers and label in ("cold","changed-cold"):
            env["TWINS_ISLAND_PROFILE"]=str(out/"collect.txt")
        t=time.monotonic_ns()
        with (out/"run.log").open("wb") as log:
            rc=subprocess.run(list(map(str,cmd)),cwd=out,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=1200).returncode
        files={}
        for part in ("messages.fct","actions.dag","collect.txt"):
            path=out/part
            if path.exists():
                with path.open("rb") as f: files[part]=hashlib.file_digest(f,"sha256").hexdigest()
        (out/"receipt.json").write_text(json.dumps(dict(command=list(map(str,cmd)),rc=rc,wall_ns=time.monotonic_ns()-t,files=files,inputs=inputs),indent=2)+"\n")
        print(name,workers,label,rc,flush=True)
