"""Recompile only the scenario on the historical optimized Unison libraries."""
from pathlib import Path
import json
import shlex
import subprocess

VIDEOS = Path.home() / 'Videos'
TREE = VIDEOS / 'netsim/unison'
CANDIDATE = VIDEOS / 'twins-paper-followup-20261123/candidate'

def build(output, contract=None, inspect_tcp_options=False, tree=TREE, trace_queue_reasons=False):
    output.mkdir(parents=True, exist_ok=True)
    source = output / 'twins-ref.cc'
    code = (CANDIDATE/'validation/ns3/twins-ref.cc').read_text()
    code = code.replace('#include <chrono>', '#include <chrono>\n#include <iomanip>', 1)
    code = code.replace('    Simulator::Destroy();',
        '    std::cout << "evidence event_loop_ns " << (uint64_t)(wall * 1e9) << "\\n";\n    Simulator::Destroy();',1)
    code = code.replace('BooleanValue(false));\n    Config::SetDefault("ns3::TcpSocketBase::Sack", BooleanValue(false));',
        'BooleanValue(getenv("TWINS_REF_TIMESTAMPS") != nullptr));\n'
        '    Config::SetDefault("ns3::TcpSocketBase::Sack", BooleanValue(getenv("TWINS_REF_SACK") != nullptr));',1)
    if inspect_tcp_options:
        code = code.replace('#include "ns3/hash.h"', '#include "ns3/hash.h"\n#include "ns3/tcp-option-sack.h"')
        code = code.replace('        std::cout << "rx "',
            '        if (auto sack = DynamicCast<const TcpOptionSack>(h.GetOption(TcpOption::SACK)))\n'
            '            for (auto block : sack->GetSackList())\n'
            '                std::cout << "SACK block " << Simulator::Now().GetNanoSeconds() << " " << block.first.GetValue() << " " << block.second.GetValue() << "\\n";\n'
            '        std::cout << "rx "',1)
    if trace_queue_reasons:
        code = add_queue_reasons(code)
    source.write_text(code)
    cache = tree / 'cmake-cache'
    original = tree / 'scratch/twins-ref.cc'
    old_binary = tree / 'build/scratch/ns3.46.1-twins-ref-optimized'
    entries = json.loads((cache/'compile_commands.json').read_text())
    entry = next(x for x in entries if x['file'] == str(original))
    compile_argv = shlex.split(entry['command'])
    obj = output / 'reference.o'
    compile_argv[compile_argv.index('-o')+1] = str(obj)
    compile_argv[-1] = str(source)
    compile_argv.insert(1, '-I'+str(CANDIDATE/'src'))
    if contract:
        compile_argv.insert(-1, '-ffp-contract='+contract)
    exe = output / 'reference'
    command = subprocess.check_output(['ninja','-C',str(cache),'-t','commands',str(old_binary)], text=True).splitlines()[-1]
    link_argv = [str(obj) if x=='scratch/CMakeFiles/scratch_twins-ref.dir/twins-ref.cc.o' else x
                 for x in shlex.split(command) if x not in (':','&&')]
    link_argv[link_argv.index('-o')+1] = str(exe)
    (output/'commands.json').write_text(json.dumps(dict(compile=compile_argv,link=link_argv),indent=2)+'\n')
    with (output/'build.log').open('wb') as stream:
        subprocess.run(compile_argv,cwd=entry['directory'],stdout=stream,stderr=subprocess.STDOUT,check=True)
        subprocess.run(link_argv,cwd=cache,stdout=stream,stderr=subprocess.STDOUT,check=True)
    return exe


def add_queue_reasons(code):
    anchor='            if (const char *tq = getenv("TRACE_QUEUE"))'
    if code.count(anchor)!=1:
        raise ValueError('trace observer anchor changed')
    observer=r"""
            if (const char *target = getenv("TWINS_TARGET_QUEUE"))
            {
                int a=-1,b=-1;
                sscanf(target,"%d,%d",&a,&b);
                static std::map<uint32_t,int> target_hosts;
                if (target_hosts.empty())
                    for (uint32_t i=0;i<H;++i) target_hosts[hostAddr[i].Get()] = i;
                auto trace=+[](std::string event, int a, int b, Ptr<QueueDisc> disc,
                            uint32_t node, uint32_t dev, Ptr<const QueueDiscItem> item, const char *reason) {
                    auto ip = DynamicCast<const Ipv4QueueDiscItem>(item);
                    if (!ip) return;
                    auto src=target_hosts.find(ip->GetHeader().GetSource().Get());
                    auto dst=target_hosts.find(ip->GetHeader().GetDestination().Get());
                    if (src==target_hosts.end() || dst==target_hosts.end()) return;
                    const int i=src->second, j=dst->second;
                    if (!((i==a && j==b) || (i==b && j==a))) return;
                    TcpHeader th;
                    item->GetPacket()->PeekHeader(th);
                    std::cout << "target_queue " << event << " " << Simulator::Now().GetPicoSeconds()
                              << " " << node << " " << dev << " " << i << " " << j
                              << " " << th.GetSourcePort() << " " << th.GetDestinationPort()
                              << " " << (unsigned)th.GetFlags() << " "
                              << th.GetSequenceNumber().GetValue() << " " << th.GetAckNumber().GetValue()
                              << " " << disc->GetNPackets() << " " << reason << "\n";
                };
                const uint32_t node=(*it)->GetId(), dev=d;
                q->TraceConnectWithoutContext("DropBeforeEnqueue",
                    MakeBoundCallback(trace,std::string("drop"),a,b,q,node,dev));
                q->TraceConnectWithoutContext("Mark",
                    MakeBoundCallback(trace,std::string("mark"),a,b,q,node,dev));
            }
"""
    tx='        if (p->GetSize() == 0)\n            return;\n        uint32_t seq = h.GetSequenceNumber().GetValue();'
    if code.count(tx)!=1:
        raise ValueError('socket observer anchor changed')
    code=code.replace(tx,r"""
        std::cout << "target_socket_tx " << Simulator::Now().GetPicoSeconds()
                  << " " << h.GetSourcePort() << " " << h.GetDestinationPort()
                  << " " << (unsigned)h.GetFlags() << " " << h.GetSequenceNumber().GetValue()
                  << " " << h.GetAckNumber().GetValue() << " " << p->GetSize() << "\n";
"""+tx,1)
    return code.replace(anchor,observer+anchor,1)
