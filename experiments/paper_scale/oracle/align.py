"""Apply the SimAI four-byte sequence header and integer wire clock in the live oracle only.

Link the changed author's translation units into the separate live executable. Offline
IRN validation continues to use the pinned transport without the SimAI adapter.
"""
import re,shutil,subprocess,sys
from pathlib import Path
tree,out=map(lambda p:Path(p).resolve(),sys.argv[1:])
out.mkdir(exist_ok=True,parents=True)
sources=['src/internet/model/seq-ts-header.cc','src/point-to-point/model/qbb-header.cc','src/network/utils/custom-header.cc','src/point-to-point/model/qbb-net-device.cc','src/point-to-point/model/rdma-hw.cc']
def once(s,a,b):
    assert s.count(a)==1,a
    return s.replace(a,b)
for name in sources:
    f=tree/name;s=f.read_text()
    if f.name=='seq-ts-header.cc':s=once(s,'return 6 + IntHeader','return 10 + IntHeader')
    if f.name=='qbb-header.cc':s=once(s,'return sizeof(tmp.sport)','return 4 + sizeof(tmp.sport)')
    if f.name=='custom-header.cc':
        s=once(s,'return 8 + sizeof(udp.pg)','return 12 + sizeof(udp.pg)')
        s=once(s,'return sizeof(ack.sport)','return 4 + sizeof(ack.sport)')
    for rate,sz in [('m_bps','p->GetSize()'),('qp->m_rate','pkt_size'),('qp->m_max_rate','pkt_size'),('qp->m_rate','qp->lastPktSize'),('new_rate','qp->lastPktSize')]:
        old=f'Seconds({rate}.CalculateTxTime({sz}))'
        if old in s:s=once(s,old,f'NanoSeconds(uint64_t({sz}) * 8000000000ULL / {rate}.GetBitRate())')
    if f.name=='rdma-hw.cc':
        s=once(s,'    acc_timeout_count[qp->m_flow_id]++;','    ++live_irn_timeouts;\n    acc_timeout_count[qp->m_flow_id]++;')
        s=once(s,'    qp->stat.txTotalBytes += payload_size;', '    if (qp->snd_nxt < live_high_seq[qp->m_flow_id]) ++live_irn_retx;\n    live_high_seq[qp->m_flow_id]=std::max<uint64_t>(live_high_seq[qp->m_flow_id],qp->snd_nxt+payload_size);\n    qp->stat.txTotalBytes += payload_size;')
        s=once(s,'qp->irn.m_sack.sack(ch.ack.irnNack, ch.ack.irnNackSize);','++live_irn_sacks; qp->irn.m_sack.sack(ch.ack.irnNack, ch.ack.irnNackSize);')
        s=s.replace('namespace ns3', 'extern "C" { unsigned long long live_irn_timeouts=0,live_irn_retx=0,live_irn_sacks=0; }\nstatic std::map<int,uint64_t> live_high_seq;\nnamespace ns3',1)
    src=out/f.name;src.write_text(s)
    subprocess.run(['g++','-O3','-fPIC','-std=gnu++11','-w','-DHAVE_PACKET_H=1','-DHAVE_SQLITE3=1','-DHAVE_IF_TUN_H=1','-I'+str(tree/'build'),'-I'+str(tree),'-I'+str(f.parent),'-c',src,'-o',out/(f.stem+'.o')],check=True)
