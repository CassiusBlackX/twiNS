"""Prepare high-rate two-tier IRN probes with simultaneous live sender QPs."""
from pathlib import Path
import sys

root=Path(sys.argv[1]).resolve()
src=Path(sys.argv[2]).resolve()
root.mkdir(parents=True,exist_ok=False)
topo=root/"leaf_spine_128_100G_OS2.txt"
topo.write_text((src/"irn-topo.txt").read_text().replace("100Gbps","1000Gbps"))
(root/"graph").write_bytes((src/"irn.graph").read_bytes())
conf=(src/"irn.cfg").read_text().replace("100000000000 ", "1000000000000 ")
conf="\n".join("TOPOLOGY_FILE "+str(topo) if line.startswith("TOPOLOGY_FILE ") else line for line in conf.splitlines())+"\n"
for n in (63,64,65):
    d=root/str(n);d.mkdir()
    (d/"config").write_text(conf)
    (d/"ref.flows").write_text(str(n)+"\n"+"".join(f"0 {j} 3 1000 2.0\n" for j in range(1,n+1)))
    (d/"flows.csv").write_text("src,dst,flow_size,start_ns\n"+"".join(f"0,{j},1000,2000000000\n" for j in range(1,n+1)))
