// astra-sim's system layer (SimAI, src/astra/fetch.sh) over a network of ours: what SimAI's ns-3
// frontend does between Sys and the network (network_frontend/ns3/AstraSimNetwork.cc, entry.h), on
// a queue of its own instead of ns-3's. Actions are recorded with what ns-3 would order them by:
// the event they ran in and their ordinal there (the uids the event had scheduled before them).
#pragma once
#include <cstdint>
#include <queue>
#include <string>
#include <vector>

namespace astra
{

// what an action hangs from: main (kind 'm'), a flow's QpComplete ('r') or SendComplete ('s'), a
// timer ('t')
struct Cause
{
    char kind;
    int64_t id;
};

struct Flow
{
    int src, dst;
    uint64_t size;
    Cause cause;
    uint32_t ord;
    int64_t t; // SendFlow's time
};

struct Timer
{
    int64_t t, delay;
    uint64_t uid; // (ns-3's order: timers of one time run in the order they were scheduled)
    void (*fn)(void *);
    void *arg;
    Cause cause;
    uint32_t ord;
};

// the network: told of each flow and timer as a callback makes it; it calls Frontend::qp_finish and
// send_finish back, in event order
struct Network
{
    virtual ~Network() {}
    virtual void flow_added(int64_t id, const Flow &f) = 0;
    virtual void timer_added(int64_t id, const Timer &t) {}
};

class Frontend
{
  public:
    // the topology file's first line gives the nodes; workload: AICB's
    Frontend(const std::string &topo, const std::string &workload, Network *net);
    int gpus() const { return gpu_num; }
    // Workload::fire of every rank, as SimAI's main does (cause 'm', ordinals: the uids)
    void fire();

    int64_t now = 0;
    // the next timer: the caller compares it with the network's next completion
    bool has_timer() const { return !timers_q.empty(); }
    int64_t next_timer() const { return timers_q.top().t; }
    int64_t next_timer_id() const { return timers_q.top().id; }
    void run_timer();
    // a flow's completions, at the current time (now)
    void qp_finish(int64_t flow);
    void send_finish(int64_t flow);

    std::vector<Flow> flows;
    std::vector<Timer> timers;
    bool finished = false;
    uint64_t callbacks = 0;

    struct Impl;

  private:
    friend class Net;
    friend struct Impl;
    void begin(Cause c)
    {
        cur = c;
        ord = 0;
        callbacks++;
    }
    Impl *x;
    Network *net;
    int gpu_num = 0, nodes_num = 0, nvswitch_num = 0, gpus_per_server = 0;
    Cause cur{'m', -1};
    uint32_t ord = 0;
    uint64_t next_uid = 0;
    struct TQ
    {
        int64_t t;
        uint64_t uid;
        int64_t id;
        bool operator<(const TQ &o) const { return t != o.t ? t > o.t : uid > o.uid; } // (a min-heap)
    };
    std::priority_queue<TQ> timers_q;
};

// the system layer on a network without congestion (every flow takes its bytes at the NIC's rate
// and a fixed latency): its flows, which no network changes
struct StubStats
{
    uint64_t timers, callbacks;
    int64_t simulated_ns;
    bool finished;
    double init_s, run_s;
};
StubStats stub_run(const std::string &topo, const std::string &workload, double gbps, std::vector<Flow> *flows);

} // namespace astra
