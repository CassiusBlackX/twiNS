// twins-astra-stub TOPO WORKLOAD [NIC_GBPS]: astra-sim on a network without congestion (every flow
// takes its bytes at the NIC's rate and a fixed latency): counts flows and timers, and measures
// the system layer's CPU time per callback.
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <queue>
#include <unistd.h>

#include "astra/frontend.h"

using namespace astra;

struct Stub : Network
{
    struct Ev
    {
        int64_t t;
        uint64_t seq;
        int64_t flow;
        bool recv;
        bool operator<(const Ev &o) const { return t != o.t ? t > o.t : seq > o.seq; }
    };
    std::priority_queue<Ev> q;
    uint64_t seq = 0;
    double gbps = 400;
    Frontend *fe = nullptr;
    void flow_added(int64_t id, const Flow &f) override
    {
        int64_t tx = 3000 + (int64_t)((double)f.size * 8 / gbps) + 1000;
        q.push(Ev{f.t + tx, seq++, id, false});
        q.push(Ev{f.t + tx + 1000, seq++, id, true});
    }
};

int main(int argc, char **argv)
{
    if (argc < 3)
    {
        fprintf(stderr, "usage: twins-astra-stub TOPO WORKLOAD [NIC_GBPS]\n");
        return 1;
    }
    Stub net;
    if (argc > 3)
        net.gbps = atof(argv[3]);
    auto t0 = std::chrono::steady_clock::now();
    Frontend fe(argv[1], argv[2], &net);
    fe.fire();
    auto t1 = std::chrono::steady_clock::now();
    while (!fe.finished && (fe.has_timer() || !net.q.empty()))
    {
        bool tm = fe.has_timer() && (net.q.empty() || fe.next_timer() <= net.q.top().t);
        if (tm)
            fe.run_timer();
        else
        {
            auto e = net.q.top();
            net.q.pop();
            fe.now = e.t;
            if (e.recv)
                fe.qp_finish(e.flow);
            else
                fe.send_finish(e.flow);
        }
    }
    auto t2 = std::chrono::steady_clock::now();
    double init = std::chrono::duration<double>(t1 - t0).count(), run = std::chrono::duration<double>(t2 - t1).count();
    printf("%zu flows, %zu timers, %llu callbacks, finished %d, simulated %.6f s; init %.3f s, run %.3f s: %.2f us per callback, %.2f per flow\n",
           fe.flows.size(), fe.timers.size(), (unsigned long long)fe.callbacks, (int)fe.finished, fe.now * 1e-9, init, run, run * 1e6 / fe.callbacks, run * 1e6 / fe.flows.size());
    fflush(stdout);
    _exit(0);
}
