"""Observe native SimAI RX-QP lifetime with the live system layer still running."""
from pathlib import Path
import os
import re
import subprocess

root=Path.cwd()
out=root/"simai-life-run"
out.mkdir()
ns=Path("/home/cassius/netsim/simai-trace/astra-sim-alibabacloud/extern/network_backend/ns3-interface/simulation")
model=ns/"src/point-to-point/model"
build=ns/"build"
inc=out/"include/ns3"
inc.mkdir(parents=True)
for item in (build/"include/ns3").iterdir():
    if item.is_symlink():
        target=os.readlink(item)
        if target.startswith("/simai/"):
            target="/home/cassius/netsim/simai-trace/"+target[len("/simai/"):]
        (inc/item.name).symlink_to(target)
    elif item.is_file():
        (inc/item.name).symlink_to(item)
lib=build/"lib"
source=(model/"rdma-hw.cc").read_text()
anchor="int RdmaHw::ReceiveUdp(Ptr<Packet> p, CustomHeader &ch){"
addition=r'''
static Ptr<Packet> life_pkt;
static CustomHeader *life_header = nullptr;
static RdmaHw *life_receiver = nullptr;

static bool LifeSelected(uint32_t sip, uint32_t dip)
{
    return ((sip >> 8) & 0xffffu) == 0 && ((dip >> 8) & 0xffffu) == 8;
}

static void LifeInject()
{
    if (!life_pkt) { fprintf(stderr, "SIMAI missing duplicate capture\n"); std::abort(); }
    auto ch = *life_header;
    auto qp = life_receiver->GetRxQp(ch.dip,ch.sip,ch.udp.dport,ch.udp.sport,ch.udp.pg,false);
    printf("SIMAI duplicate %llu present=%d seq=%u\n", (unsigned long long)Simulator::Now().GetTimeStep(), int(bool(qp)),ch.udp.seq);
    life_receiver->ReceiveUdp(life_pkt->Copy(), ch);
}
'''
assert source.count(anchor)==1
source=source.replace(anchor,addition+anchor+r'''
    if (!life_pkt && LifeSelected(ch.sip,ch.dip) && ch.udp.seq)
    {
        life_pkt = p->Copy();
        life_header = new CustomHeader(ch);
        life_receiver = this;
        printf("SIMAI captured %llu sport=%u seq=%u\n", (unsigned long long)Simulator::Now().GetTimeStep(),ch.udp.sport,ch.udp.seq);
        if (const char *at = getenv("TWINS_SIMAI_DUP_NS"))
            Simulator::Schedule(NanoSeconds(std::stoll(at))-Simulator::Now(), &LifeInject);
    }
''',1)
anchor="\tm_qpCompleteCallback(qp);"
assert source.count(anchor)==1
source=source.replace(anchor,anchor+r'''
    if (life_header && LifeSelected(qp->sip.Get(),qp->dip.Get()) && qp->sport == life_header->udp.sport)
    {
        printf("SIMAI close %llu sport=%u\n", (unsigned long long)Simulator::Now().GetTimeStep(),qp->sport);
        if (getenv("TWINS_SIMAI_DUP_AFTER_CLOSE")) Simulator::ScheduleNow(&LifeInject);
    }
''',1)
(out/"rdma-hw-audit.cc").write_text("#include <cstdlib>\n"+source)
objects=list((ns/"cmake-cache/src/point-to-point/CMakeFiles/libpoint-to-point-obj.dir").rglob("*.o"))
assert len(objects)>8
name="libns3.36.1-point-to-point-optimized.so"
with (out/"build.log").open("wb") as log:
    subprocess.run(["g++","-O3","-DNDEBUG","-std=gnu++17","-fPIC","-fno-semantic-interposition",
                    "-include","cstdint",f"-I{inc.parent}",f"-I{model}","-c",out/"rdma-hw-audit.cc",
                    "-o",out/"rdma-hw.o"],stdout=log,stderr=log,check=True,timeout=150)
    subprocess.run(["g++","-shared",f"-Wl,-soname,{name}", "-o", out/name,out/"rdma-hw.o",
                    *sorted(p for p in objects if p.name!="rdma-hw.cc.o"),
                    "-Wl,--no-as-needed",*[lib/f"libns3.36.1-{part}-optimized.so" for part in ("internet","network","core","traffic-control")]],
                    stdout=log,stderr=log,check=True,timeout=150)
    # Avoid loading the installed original under a second path.
binary=build/"scratch/ns3.36.1-AstraSimNetwork-optimized"
topo=Path("/home/cassius/netsim/aiwork/topo/DCN+SingleToR_128g_8gps_200Gbps_A100")
base=(root/"config.log").read_text()
wl=(root/"workload.log").read_text().replace("8388608","1048576")
header,_,msg=wl.splitlines()
(out/"workload").write_text(header+"\n2\n"+msg+"\n"+msg.replace("coll_layer","coll_extra")+"\n")
def run(tag,**extra):
    d=out/tag
    d.mkdir()
    cfg=re.sub(r"(?m)^BUFFER_SIZE\s+\d+", "BUFFER_SIZE 64",base)
    cfg=re.sub(r"(?m)^(\S+\s+)/etc/[^\s]+/([^/\s]+)$",r"\1"+str(d)+r"/\2",cfg)
    (d/"config").write_text(cfg)
    env={**os.environ,"LD_LIBRARY_PATH":str(out)+":"+str(lib),"AS_SEND_LAT":"3","TWINS_TRACE":str(d/"events.trace"),
         "TWINS_TXTRACE":str(d/"wire.raw"),"TWINS_TXUNTIL":"1000000",**extra}
    with (d/"run.log").open("wb") as log:
        rc=subprocess.run([binary,"-t","1","-w",out/"workload","-n",topo,"-c",d/"config"],
                          cwd=d,env=env,stdout=log,stderr=log,timeout=120).returncode
    (d/"rc").write_text(str(rc)+"\n")
    print(tag,rc,flush=True)
    return (d/"run.log").read_text(errors="replace")
control=run("control")
match=re.search(r"SIMAI close (\d+)",control)
if not match: raise ValueError("baseline did not complete the capture connection")
at=int(match[1])
run("before",TWINS_SIMAI_DUP_NS=str(at-1))
run("equal-before",TWINS_SIMAI_DUP_NS=str(at))
run("equal-after",TWINS_SIMAI_DUP_AFTER_CLOSE="1")
run("after",TWINS_SIMAI_DUP_NS=str(at+1))
