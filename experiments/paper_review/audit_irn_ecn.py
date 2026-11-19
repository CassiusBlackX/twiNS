"""Build a ConWeave switch observation for the previously located 128-GPU ECN fork."""
import argparse
from pathlib import Path
import subprocess

parser=argparse.ArgumentParser()
parser.add_argument("tree",type=Path)
parser.add_argument("out",type=Path)
args=parser.parse_args()
tree=args.tree.resolve()
out=args.out.resolve()
out.mkdir(parents=True,exist_ok=True)
model=tree/"src/point-to-point/model"
stem="switch-mmu"
source=(model/(stem+".cc")).read_text()
before="        if (m_uniform_random_var.GetValue(0, 1) < p) return true;"
after=r'''        const double draw = m_uniform_random_var.GetValue(0, 1);
        if (node_id == 156 && Simulator::Now().GetTimeStep() <= 22441)
            fprintf(stderr, "sw-draw %lld %u %u %.17g %.17g %d %u %u %.17g\n",
                    (long long)Simulator::Now().GetTimeStep(), ifindex,
                    m_usedEgressQSharedBytes[ifindex][qIndex], draw, p, int(draw < p),
                    kmin[ifindex], kmax[ifindex], pmax[ifindex]);
        if (draw < p) return true;'''
if source.count(before)!=1:
    raise ValueError("author ECN source changed")
generated=out/(stem+".cc")
generated.write_text(source.replace(before,after))
subprocess.run(["g++","-O3","-fPIC","-std=gnu++11","-w",
                "-DHAVE_PACKET_H=1","-DHAVE_SQLITE3=1","-DHAVE_IF_TUN_H=1",
                f"-I{tree/'build'}",f"-I{tree}",f"-I{model}","-c",generated,
                "-o",out/(stem+".o")],check=True)
