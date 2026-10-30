#!/usr/bin/env python3
"""Instruments a copy of simai-ref (seq build, simai.patch) for TwiNS's DAG extraction (phase 3).

usage: trace_patch.py SIMAI_TREE

With TWINS_TRACE=FILE set, the AstraSimNetwork binary writes one line per astra-sim action, with
the ns-3 event that took it (the cause) and the action's ordinal in that event (the events the
event had scheduled before, from ns-3's uid counter):

  F id src dst bytes sport send_lat_ns cause cause_id ord now_ns flow_id channel   SendFlow, per
                                                                    queue pair (flow_id: SimCCL's)
  T id delay_ns cause cause_id ord now_ns                          sim_schedule
  C cause cause_id ord0 ord1                                       a callback ran: its event's
                                                                    ordinals at entry and exit

cause: m (main, before Simulator::Run: ord is the global uid), r (the QpComplete of flow
cause_id), s (its first SendComplete that completed the send), t (timer cause_id fired).
Nothing changes without TWINS_TRACE (the timers' events are the same events either way).

TWINS_TXTRACE=FILE (TWINS_TXFROM, TWINS_TXUNTIL in ns): every transmission start, as
simai-dag-ref's --txtrace ("ns node dev kind src dst sport seq bytes ecn").

It also makes the frontend's network setup reproducible as simai-ref's is: the topology maps are
ordered by node id (SimAI's by Ptr<Node>, a heap address, which orders ECMP next hops), and each
switch port draws its ECN marks from its own stream (simai.patch; SimAI never configures them).

And it widens ns-3's event uids to 64 bits (TWINS_UID64). ns-3.36 keeps them in 32 bits
(Scheduler::EventKey::m_uid, DefaultSimulatorImpl::m_uid), and events at equal times run in uid
order. After 2^32 events the counter wraps, and events scheduled later run first among
simultaneous ones: a 1024-GPU GPT-3 13B probe passes 2^32 events at 44.6 ms. Below 2^32 events
nothing changes.
"""
import sys, os

root = sys.argv[1]
sim = os.path.join(root, "astra-sim-alibabacloud/extern/network_backend/ns3-interface/simulation")


def patch(path, pairs, marker="TwinsOrd"):
    s = open(path).read()
    if marker in s:
        print("already patched:", path, marker)
        return
    for a, b in pairs:
        assert s.count(a) == 1, (path, a)
        s = s.replace(a, b)
    open(path, "w").write(s)
    print("patched", path)


patch(os.path.join(sim, "src/core/model/default-simulator-impl.cc"), [
    ("""DefaultSimulatorImpl::DefaultSimulatorImpl ()
{""", """// TwiNS trace (TWINS_TRACE): the events the running event has scheduled (before Run: the uid)
static uint32_t *g_twUid = nullptr;
static uint32_t g_twEv0 = 0;
uint64_t TwinsOrd (void)
{
  return g_twUid ? *g_twUid - g_twEv0 : 0;
}

DefaultSimulatorImpl::DefaultSimulatorImpl ()
{
  g_twUid = &m_uid;"""),
    ("""  m_currentUid = next.key.m_uid;
  next.impl->Invoke ();""", """  m_currentUid = next.key.m_uid;
  g_twEv0 = m_uid;
  next.impl->Invoke ();"""),
])

ent = os.path.join(sim, "scratch/entry.h")
patch(ent, [
    ("""#include "SimCCL/mock/MockNcclLog.h"
""", """#include "SimCCL/mock/MockNcclLog.h"
#include <tuple>

// TwiNS trace (validation/simai/trace_patch.py)
namespace ns3 { uint64_t TwinsOrd (void); }
static FILE *tw_out = nullptr;
static char tw_kind = 'm';
static long tw_id = -1;
static long tw_nflow = 0, tw_ntimer = 0;
static std::map<std::tuple<uint32_t, uint32_t, uint32_t>, long> tw_flow_of; // (sport, src, dst)
static void tw_init () {
  const char *p = getenv ("TWINS_TRACE");
  if (p)
    tw_out = fopen (p, "w");
}
// a callback's cause, for the actions in it
struct TwCause {
  char k;
  long id;
  uint64_t o0;
  TwCause (char k_, long i_) : k (tw_kind), id (tw_id) {
    tw_kind = k_;
    tw_id = i_;
    o0 = ns3::TwinsOrd ();
  }
  ~TwCause () {
    if (tw_out)
      fprintf (tw_out, "C %c %ld %llu %llu\\n", tw_kind, tw_id, (unsigned long long) o0, (unsigned long long) ns3::TwinsOrd ());
    tw_kind = k;
    tw_id = id;
  }
};
static void tw_fire (long id, void (*f) (void *), void *a) {
  TwCause c ('t', id);
  f (a);
}
static long tw_flow (uint32_t sport, uint32_t src, uint32_t dst) {
  auto it = tw_flow_of.find (std::make_tuple (sport, src, dst));
  return it == tw_flow_of.end () ? -1 : it->second;
}
"""),
    ("""  if(real_PacketCount == 0) real_PacketCount = 1;
""", """  if(real_PacketCount == 0) real_PacketCount = 1;
  if (tw_out) {
    long id = tw_nflow++;
    tw_flow_of[std::make_tuple ((uint32_t) port, (uint32_t) src, (uint32_t) dst)] = id;
    fprintf (tw_out, "F %ld %d %d %llu %u %lld %c %ld %llu %lld %d %d\\n", id, src, dst, (unsigned long long) real_PacketCount, port,
             (long long) send_lat / 1000, tw_kind, tw_id, (unsigned long long) ns3::TwinsOrd (),
             (long long) Simulator::Now ().GetNanoSeconds (), request->flowTag.current_flow_id, tag);
  }
"""),
    ("""void qp_finish(FILE *fout, Ptr<RdmaQueuePair> q) {""", """void qp_finish0(FILE *fout, Ptr<RdmaQueuePair> q);
void qp_finish(FILE *fout, Ptr<RdmaQueuePair> q) {
  TwCause c ('r', tw_out ? tw_flow (q->sport, ip_to_node_id (q->sip), ip_to_node_id (q->dip)) : -1);
  qp_finish0 (fout, q);
}
void qp_finish0(FILE *fout, Ptr<RdmaQueuePair> q) {"""),
    ("""void send_finish(FILE *fout, Ptr<RdmaQueuePair> q) {""", """void send_finish0(FILE *fout, Ptr<RdmaQueuePair> q);
void send_finish(FILE *fout, Ptr<RdmaQueuePair> q) {
  TwCause c ('s', tw_out ? tw_flow (q->sport, ip_to_node_id (q->sip), ip_to_node_id (q->dip)) : -1);
  send_finish0 (fout, q);
}
void send_finish0(FILE *fout, Ptr<RdmaQueuePair> q) {"""),
])

net = os.path.join(sim, "scratch/AstraSimNetwork.cc")
patch(net, [
    ("""    Simulator::Schedule(NanoSeconds(t.schTime), t.msg_handler, t.fun_arg);
    return;""", """    if (tw_out) {
      long id = tw_ntimer++;
      fprintf (tw_out, "T %ld %.17g %c %ld %llu %lld\\n", id, t.schTime, tw_kind, tw_id,
               (unsigned long long) ns3::TwinsOrd (), (long long) Simulator::Now ().GetNanoSeconds ());
      Simulator::Schedule(NanoSeconds(t.schTime), &tw_fire, id, t.msg_handler, t.fun_arg);
      return;
    }
    Simulator::Schedule(NanoSeconds(t.schTime), t.msg_handler, t.fun_arg);
    return;"""),
    ("""  struct user_param user_param;
  MockNcclLog::set_log_name("SimAI.log");""", """  struct user_param user_param;
  tw_init ();
  MockNcclLog::set_log_name("SimAI.log");"""),
])

# the transmission trace
patch(net, [
    ("""  main1(user_param.network_topo,user_param.network_conf);
""", """  main1(user_param.network_topo,user_param.network_conf);
  if (getenv ("TWINS_TXTRACE")) {
    tw_tx = fopen (getenv ("TWINS_TXTRACE"), "w");
    if (getenv ("TWINS_TXFROM"))
      tw_tx_from = strtoull (getenv ("TWINS_TXFROM"), nullptr, 10);
    if (getenv ("TWINS_TXUNTIL"))
      tw_tx_until = strtoull (getenv ("TWINS_TXUNTIL"), nullptr, 10);
    for (uint32_t i = 0; i < n.GetN (); i++)
      for (uint32_t j = 0; j < n.Get (i)->GetNDevices (); j++) {
        Ptr<QbbNetDevice> dev = DynamicCast<QbbNetDevice> (n.Get (i)->GetDevice (j));
        if (dev)
          dev->TraceConnectWithoutContext ("PhyTxBegin", MakeBoundCallback (&tw_tx_begin, i, j));
      }
  }
"""),
    ("""int main(int argc, char *argv[]) {""", """// TWINS_TXTRACE: as simai-dag-ref's --txtrace
static FILE *tw_tx = nullptr;
static uint64_t tw_tx_from = 0, tw_tx_until = ~0ull;
static void tw_tx_begin (uint32_t node, uint32_t dev, Ptr<const Packet> p) {
  uint64_t now = Simulator::Now ().GetTimeStep ();
  if (now < tw_tx_from || now > tw_tx_until)
    return;
  CustomHeader ch (CustomHeader::L2_Header | CustomHeader::L3_Header | CustomHeader::L4_Header);
  ch.getInt = 1;
  p->PeekHeader (ch);
  char kind = '?';
  uint32_t a = ip_to_node_id (Ipv4Address (ch.sip)), b = ip_to_node_id (Ipv4Address (ch.dip)), sport = 0;
  uint64_t seq = 0;
  if (ch.l3Prot == 0x11)
    kind = 'D', sport = ch.udp.sport, seq = ch.udp.seq;
  else if (ch.l3Prot == 0xFC || ch.l3Prot == 0xFD)
    kind = ch.l3Prot == 0xFC ? 'A' : 'N', sport = ch.ack.dport, seq = ch.ack.seq;
  else if (ch.l3Prot == 0xFE)
    kind = ch.pfc.time > 0 ? 'P' : 'R', a = ch.pfc.qIndex, b = 0;
  fprintf (tw_tx, "%lu %u %u %c %u %u %u %lu %u %u\\n", now, node, dev, kind, a, b, sport, seq, p->GetSize (),
           (unsigned) (ch.m_tos & 3));
}

int main(int argc, char *argv[]) {"""),
], marker="TWINS_TXTRACE")

# the topology maps ordered by node id, as simai-ref (SimAI orders them by Ptr<Node>, a heap
# address: the order of a node's next hops, so ECMP, would follow the allocator): ById in every
# map keyed by Ptr<Node>
def by_id(path):
    s = open(path).read()
    if "TWINS_BYID" in s:
        print("already patched:", path, "TWINS_BYID")
        return
    key, ends, j = "map<Ptr<Node>,", [], s.find("map<Ptr<Node>,")
    while j >= 0:
        k, depth = j + 3, 0  # at the map's "<": its closing ">"
        while True:
            depth += s[k] == "<"
            depth -= s[k] == ">"
            if depth == 0:
                break
            k += 1
        ends.append(k)
        j = s.find(key, j + 1)
    for k in sorted(ends, reverse=True):
        s = s[:k] + ", ById" + s[k:]
    first = s.find(key)
    line = s.rfind("\n", 0, first) + 1
    s = s[:line] + """// TWINS_BYID (trace_patch.py): maps of nodes ordered by node id
struct ById {
  bool operator() (const Ptr<Node> &a, const Ptr<Node> &b) const { return a->GetId () < b->GetId (); }
};
""" + s[line:]
    open(path, "w").write(s)
    print("patched", path, "TWINS_BYID")


by_id(os.path.join(sim, "scratch/common.h"))

# each switch port's ECN marks from its own stream, as simai-ref (simai.patch adds the streams;
# SimAI's frontend never configures them, so it draws from a new UniformVariable per mark, whose
# stream depends on how many were made before)
patch(os.path.join(sim, "scratch/common.h"), [
    ("""        sw->m_mmu->ConfigEcn(j, rate2kmin[rate], rate2kmax[rate],
                             rate2pmax[rate]);
""", """        sw->m_mmu->ConfigEcn(j, rate2kmin[rate], rate2kmax[rate],
                             rate2pmax[rate]);
        sw->m_mmu->ConfigEcnStream(j, (uint64_t)i * SwitchMmu::pCnt + j); // TWINS_ECNSTREAM
"""),
], marker="TWINS_ECNSTREAM")

# 64-bit event uids (TWINS_UID64): ns-3.36's 32-bit uid wraps after 2^32 events (above)
core = os.path.join(sim, "src/core/model")
patch(os.path.join(core, "scheduler.h"), [
    ("""    uint32_t m_uid;        /**< Event unique id. */""",
     """    uint64_t m_uid;        /**< Event unique id. (TWINS_UID64: no wrap after 2^32 events) */"""),
], marker="TWINS_UID64")
patch(os.path.join(core, "default-simulator-impl.h"), [
    ("""  uint32_t m_uid;""", """  uint64_t m_uid; // TWINS_UID64"""),
    ("""  uint32_t m_currentUid;""", """  uint64_t m_currentUid;"""),
], marker="TWINS_UID64")
patch(os.path.join(core, "event-id.h"), [
    ("""EventId (const Ptr<EventImpl> &impl, uint64_t ts, uint32_t context, uint32_t uid);""",
     """EventId (const Ptr<EventImpl> &impl, uint64_t ts, uint32_t context, uint64_t uid); // TWINS_UID64"""),
    ("""  uint32_t GetUid (void) const;""", """  uint64_t GetUid (void) const;"""),
    ("""  uint32_t m_uid;              /**< The unique id. */""", """  uint64_t m_uid;              /**< The unique id. */"""),
], marker="TWINS_UID64")
patch(os.path.join(core, "event-id.cc"), [
    ("""EventId::EventId (const Ptr<EventImpl> &impl, uint64_t ts, uint32_t context, uint32_t uid)""",
     """EventId::EventId (const Ptr<EventImpl> &impl, uint64_t ts, uint32_t context, uint64_t uid) // TWINS_UID64"""),
    ("""uint32_t
EventId::GetUid (void) const""", """uint64_t
EventId::GetUid (void) const"""),
], marker="TWINS_UID64")
patch(os.path.join(sim, "src/core/model/default-simulator-impl.cc"), [
    ("""static uint32_t *g_twUid = nullptr;
static uint32_t g_twEv0 = 0;""", """static uint64_t *g_twUid = nullptr; // TWINS_UID64
static uint64_t g_twEv0 = 0;"""),
], marker="TWINS_UID64")
