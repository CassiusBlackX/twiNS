"""Build a CPU-only stub that measures vector growth during flow pre-enumeration."""
import argparse
from pathlib import Path
import subprocess

p=argparse.ArgumentParser(description=__doc__)
for name in ("project","astra","library","output"):
    p.add_argument(name,type=lambda value:Path(value).resolve())
a=p.parse_args()
a.output.mkdir(parents=True,exist_ok=False)
source=(a.project/"src/astra/frontend.cc").read_text()
token="    fe->flows.push_back(f);\n    flow_tags.push_back(req->flowTag);"
assert source.count(token)==1
replacement="""    const bool grow_flow = fe->flows.size() == fe->flows.capacity();
    const bool grow_tag = flow_tags.size() == flow_tags.capacity();
    const auto grow_start = grow_flow || grow_tag ? std::chrono::steady_clock::now() : std::chrono::steady_clock::time_point{};
    fe->flows.push_back(f);
    flow_tags.push_back(req->flowTag);
    if (grow_flow || grow_tag)
    {
        growth_ns += std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - grow_start).count();
        ++growth_count;
    }"""
source=source.replace(token,replacement).replace("namespace astra\n{","namespace astra\n{\nstatic uint64_t growth_ns = 0, growth_count = 0;",1)
token="    *flows = fe.flows;"
assert source.count(token)==1
source=source.replace(token,"""    *flows = fe.flows;
    fprintf(stderr, "stub vectors: %llu expansions in %.6f s; final record allocation and copy %.6f s; %zu bytes per record\\n",
            (unsigned long long)growth_count, growth_ns * 1e-9,
            std::chrono::duration<double>(std::chrono::steady_clock::now() - t2).count(), sizeof(Flow));""")
audit=a.output/"frontend-audit.cc"
audit.write_text(source)
cmd=["g++","-O3","-std=c++17",f"-I{a.project/'src'}",f"-I{a.astra/'SimAI/astra-sim-alibabacloud'}",
     f"-I{a.astra/'inc'}",str(audit),str(a.project/"src/astra/stub.cc"),str(a.library),"-pthread","-o",str(a.output/"stub")]
with (a.output/"build.log").open("wb") as log:
    subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=180)
