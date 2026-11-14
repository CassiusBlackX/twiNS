# Patches the HPCC fork's ns-3.18 tree (simulation/) in place: NS3_EVLOG=FILE writes the same 24-byte
# records as ns3_evlog_patch.py does for ns-3.46.1 ('S' at every Schedule: uid, parent = the running
# event's uid or 0 at set-up, context, time stamp; 'X' at every executed event). Unset: no change.
# usage: python3 hpcc_evlog_patch.py HPCC/simulation
import sys, os
root = sys.argv[1]
p = os.path.join(root, 'src/core/model/default-simulator-impl.cc')
s = open(p).read()
def rep(a, b, n=1):
    global s
    assert s.count(a) == n, (a[:70], s.count(a))
    s = s.replace(a, b)

rep('#include <cmath>\n', '#include <cmath>\n#include <cstdio>\n#include <cstdlib>\n')
rep('namespace ns3 {\n', '''namespace ns3 {

// (audit patch) NS3_EVLOG=FILE: kind 'S' at every Schedule (uid, parent, context, time stamp), 'X' at
// every executed event (uid, 0, context, time stamp)
struct EvRec
{
  uint8_t kind;
  uint8_t pad[3];
  uint32_t uid;
  uint32_t parent;
  uint32_t context;
  uint64_t ts;
};
struct EvLog
{
  FILE *f;
  EvLog () : f (0)
  {
    const char *p = std::getenv ("NS3_EVLOG");
    if (p)
      {
        f = std::fopen (p, "wb");
        if (f)
          std::setvbuf (f, 0, _IOFBF, 1 << 24);
      }
  }
  ~EvLog () { if (f) std::fclose (f); }
  void Put (uint8_t kind, uint32_t uid, uint32_t parent, uint32_t context, uint64_t ts)
  {
    if (!f)
      return;
    EvRec r;
    r.kind = kind;
    r.pad[0] = r.pad[1] = r.pad[2] = 0;
    r.uid = uid;
    r.parent = parent;
    r.context = context;
    r.ts = ts;
    std::fwrite (&r, sizeof r, 1, f);
  }
};
static EvLog g_evlog;
''')
# executed events
rep('''  m_currentUid = next.key.m_uid;
  next.impl->Invoke ();''', '''  m_currentUid = next.key.m_uid;
  g_evlog.Put ('X', next.key.m_uid, 0, next.key.m_context, next.key.m_ts);
  next.impl->Invoke ();''')
# every insertion made from the main thread: Schedule, ScheduleWithContext (both builds), ScheduleNow
ins = '''  ev.key.m_uid = m_uid;
  m_uid++;
  m_unscheduledEvents++;
  m_events->Insert (ev);'''
n = s.count(ins)
assert n >= 3, n
s = s.replace(ins, '''  ev.key.m_uid = m_uid;
  m_uid++;
  m_unscheduledEvents++;
  g_evlog.Put ('S', ev.key.m_uid, m_currentUid, ev.key.m_context, ev.key.m_ts);
  m_events->Insert (ev);''')
ins2 = '''      ev.key.m_uid = m_uid;
      m_uid++;
      m_unscheduledEvents++;
      m_events->Insert (ev);'''
assert s.count(ins2) == 1, s.count(ins2)
s = s.replace(ins2, '''      ev.key.m_uid = m_uid;
      m_uid++;
      m_unscheduledEvents++;
      g_evlog.Put ('S', ev.key.m_uid, m_currentUid, ev.key.m_context, ev.key.m_ts);
      m_events->Insert (ev);''')
open(p, 'w').write(s)
print("patched: %d + 1 insertion sites" % n)
