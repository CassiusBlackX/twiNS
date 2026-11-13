# Patches an ns-3.46.1 (Unison-for-ns-3) tree in place: NS3_TIE_SEED and NS3_EVLOG (see below).
import sys, os
root = sys.argv[1]
def edit(path, pairs):
    p = os.path.join(root, path); s = open(p).read()
    for a, b in pairs:
        assert s.count(a) == 1, (path, a[:60], s.count(a))
        s = s.replace(a, b)
    open(p, 'w').write(s)

edit('src/core/model/scheduler.h', [
("""inline bool
operator<(const Scheduler::EventKey& a, const Scheduler::EventKey& b)
{
    return (a.m_ts < b.m_ts || (a.m_ts == b.m_ts && a.m_uid < b.m_uid));
}""",
"""// (audit patch) NS3_TIE_SEED=n, n > 0: simultaneous events run in a pseudo-random order
// (seeded hash of the uid, then the uid); 0 or unset: ns-3's order (by uid)
extern uint32_t g_tieSeed;

inline uint32_t
TieKey(uint32_t uid)
{
    if (!g_tieSeed)
    {
        return 0;
    }
    uint64_t z = uid + 0x9e3779b97f4a7c15ULL * g_tieSeed;
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    return (uint32_t)(z ^ (z >> 31));
}

// (uid a) runs before (uid b) at one time stamp
inline bool
TieLess(uint32_t a, uint32_t b)
{
    uint32_t ka = TieKey(a), kb = TieKey(b);
    return ka < kb || (ka == kb && a < b);
}

inline bool
operator<(const Scheduler::EventKey& a, const Scheduler::EventKey& b)
{
    return (a.m_ts < b.m_ts || (a.m_ts == b.m_ts && TieLess(a.m_uid, b.m_uid)));
}"""),
("""    return (a.m_ts > b.m_ts || (a.m_ts == b.m_ts && a.m_uid > b.m_uid));""",
"""    return (a.m_ts > b.m_ts || (a.m_ts == b.m_ts && TieLess(b.m_uid, a.m_uid)));"""),
])

edit('src/core/model/scheduler.cc', [
("""namespace ns3
{
""", """namespace ns3
{

static uint32_t
TieSeedFromEnv()
{
    const char* v = std::getenv("NS3_TIE_SEED");
    return v ? (uint32_t)std::strtoul(v, nullptr, 10) : 0;
}

uint32_t g_tieSeed = TieSeedFromEnv();
"""),
])
p = os.path.join(root, 'src/core/model/scheduler.cc'); s = open(p).read()
if '#include <cstdlib>' not in s:
    s = s.replace('#include "scheduler.h"', '#include "scheduler.h"\n\n#include <cstdlib>', 1)
    open(p, 'w').write(s)

edit('src/core/model/default-simulator-impl.cc', [
("""namespace ns3
{
""", """namespace ns3
{

// (audit patch) NS3_EVLOG=FILE: a binary log of 24-byte records: kind 'S' at every Schedule
// (uid, parent = the uid of the event running, 0 at set-up, context, time stamp) and 'X' at every
// executed event (uid, 0, context, time stamp)
struct EvRec
{
    uint8_t kind;
    uint8_t pad[3];
    uint32_t uid;
    uint32_t parent;
    uint32_t context;
    uint64_t ts;
};

static_assert(sizeof(EvRec) == 24, "EvRec");

struct EvLog
{
    FILE* f = nullptr;

    EvLog()
    {
        const char* p = std::getenv("NS3_EVLOG");
        if (p)
        {
            f = std::fopen(p, "wb");
            if (f)
            {
                std::setvbuf(f, nullptr, _IOFBF, 1 << 24);
            }
        }
    }

    ~EvLog()
    {
        if (f)
        {
            std::fclose(f);
        }
    }

    void Put(uint8_t kind, uint32_t uid, uint32_t parent, uint32_t context, uint64_t ts)
    {
        if (!f)
        {
            return;
        }
        EvRec r{kind, {0, 0, 0}, uid, parent, context, ts};
        std::fwrite(&r, sizeof r, 1, f);
    }
};

static EvLog g_evlog;
"""),
("""    m_currentUid = next.key.m_uid;
    next.impl->Invoke();""", """    m_currentUid = next.key.m_uid;
    g_evlog.Put('X', next.key.m_uid, 0, next.key.m_context, next.key.m_ts);
    next.impl->Invoke();"""),
("""    ev.key.m_context = GetContext();
    ev.key.m_uid = m_uid;
    m_uid++;
    m_unscheduledEvents++;
    m_events->Insert(ev);
    return EventId(event, ev.key.m_ts, ev.key.m_context, ev.key.m_uid);""",
"""    ev.key.m_context = GetContext();
    ev.key.m_uid = m_uid;
    m_uid++;
    m_unscheduledEvents++;
    g_evlog.Put('S', ev.key.m_uid, m_currentUid, ev.key.m_context, ev.key.m_ts);
    m_events->Insert(ev);
    return EventId(event, ev.key.m_ts, ev.key.m_context, ev.key.m_uid);"""),
("""        ev.key.m_context = context;
        ev.key.m_uid = m_uid;
        m_uid++;
        m_unscheduledEvents++;
        m_events->Insert(ev);
    }
    else""", """        ev.key.m_context = context;
        ev.key.m_uid = m_uid;
        m_uid++;
        m_unscheduledEvents++;
        g_evlog.Put('S', ev.key.m_uid, m_currentUid, ev.key.m_context, ev.key.m_ts);
        m_events->Insert(ev);
    }
    else"""),
("""           (id.GetTs() == m_currentTs && id.GetUid() <= m_currentUid) ||""",
"""           (id.GetTs() == m_currentTs && !TieLess(m_currentUid, id.GetUid())) ||"""),
])
p = os.path.join(root, 'src/core/model/default-simulator-impl.cc'); s = open(p).read()
if '#include <cstdio>' not in s:
    s = s.replace('#include "default-simulator-impl.h"', '#include "default-simulator-impl.h"\n\n#include <cstdio>\n#include <cstdlib>', 1)
    open(p, 'w').write(s)
print("patched")
