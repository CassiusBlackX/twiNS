#include "astra/frontend.h"

#include <cassert>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <map>
#include <utility>

#include "SimCCL/mock/MockNcclLog.h"
#include "astra-sim/system/RecvPacketEventHadndlerData.hh"
#include "astra-sim/system/SendPacketEventHandlerData.hh"
#include "astra-sim/system/Sys.hh"

namespace astra
{

// entry.h's state, and what the calls of AstraNetworkAPI do with it
struct Frontend::Impl
{
    struct Task
    {
        int src, dest;
        uint64_t count;
        void *fun_arg;
        void (*msg_handler)(void *);
    };
    using PK = std::pair<int, std::pair<int, int>>;
    static PK pk(int a, int b, int c) { return {a, {b, c}}; }
    std::vector<AstraSim::ncclFlowTag> flow_tags;
    std::map<std::pair<std::pair<int, int>, int>, AstraSim::ncclFlowTag> receiver_pending_queue;
    std::map<PK, Task> expeRecvHash, sentHash;
    std::map<PK, uint64_t> recvHash, received_chunksize, sent_chunksize;
    std::map<PK, int> waiting_to_sent_callback, waiting_to_notify_receiver;
    std::vector<AstraSim::AstraNetworkAPI *> networks;
    std::vector<AstraSim::Sys *> systems;
    void add_timer(Frontend *f, AstraSim::timespec_t delta, void (*fn)(void *), void *arg);
    void send(Frontend *f, int rank, int dst, uint64_t count, int tag, AstraSim::sim_request *req, void (*h)(void *), void *arg);
    void recv(int rank, int src, uint64_t count, void (*h)(void *), void *arg);
    void notify_receiver(int sender, int receiver, uint64_t size, const AstraSim::ncclFlowTag &tag);
    void notify_sender(int sender, int receiver, uint64_t size, const AstraSim::ncclFlowTag &tag);
};
using PK = Frontend::Impl::PK;
using Task = Frontend::Impl::Task;
static PK pk(int a, int b, int c) { return Frontend::Impl::pk(a, b, c); }

class Net : public AstraSim::AstraNetworkAPI
{
    Frontend *f;

  public:
    Net(int rank, Frontend *f) : AstraNetworkAPI(rank), f(f) {}
    int sim_comm_size(AstraSim::sim_comm, int *) override { return 0; }
    int sim_finish() override
    {
        f->finished = true; // (SimAI's: exit(0); the run's outputs are ours to write)
        return 0;
    }
    double sim_time_resolution() override { return 0; }
    int sim_init(AstraSim::AstraMemoryAPI *) override { return 0; }
    AstraSim::timespec_t sim_get_time() override
    {
        AstraSim::timespec_t t;
        t.time_val = (double)f->now;
        return t;
    }
    void sim_schedule(AstraSim::timespec_t delta, void (*fn)(void *), void *arg) override { f->x->add_timer(f, delta, fn, arg); }
    int sim_send(void *, uint64_t count, int, int dst, int tag, AstraSim::sim_request *req, void (*h)(void *), void *arg) override
    {
        f->x->send(f, rank, dst, count, tag, req, h, arg);
        return 0;
    }
    int sim_recv(void *, uint64_t count, int, int src, int, AstraSim::sim_request *, void (*h)(void *), void *arg) override
    {
        f->x->recv(rank, src, count, h, arg);
        return 0;
    }
};

Frontend::Frontend(const std::string &topo, const std::string &workload, Network *n) : x(new Impl), net(n)
{
    std::ifstream in(topo);
    int node_num, switch_num, link_num;
    std::string type;
    in >> node_num >> gpus_per_server >> nvswitch_num >> switch_num >> link_num >> type;
    if (!in)
    {
        fprintf(stderr, "%s: no topology\n", topo.c_str());
        exit(1);
    }
    GPUType gt = type == "A100" ? GPUType::A100 : type == "A800" ? GPUType::A800 : type == "H100" ? GPUType::H100 : type == "H800" ? GPUType::H800 : GPUType::NONE;
    nodes_num = node_num - switch_num;
    gpu_num = nodes_num - nvswitch_num;
    std::vector<int> nvs;
    for (int i = gpu_num; i < nodes_num; i++)
        nvs.push_back(i);
    MockNcclLog::set_log_name("SimAI.log");
    for (int j = 0; j < nodes_num; j++)
    {
        auto *net_j = new Net(j, this);
        auto *sys = new AstraSim::Sys(net_j, nullptr, j, 0, 1, {nodes_num}, {1}, "", workload, 1, 1, 1, 1, 0, "./ncclFlowModel_", "test1", true, false, gt, {gpu_num}, nvs, gpus_per_server);
        sys->nvswitch_id = j < gpu_num ? gpu_num + j / gpus_per_server : j;
        sys->num_gpus = nodes_num - nvswitch_num;
        x->networks.push_back(net_j);
        x->systems.push_back(sys);
    }
}

void Frontend::fire()
{
    cur = Cause{'m', -1};
    ord = 0;
    for (auto *s : x->systems)
        s->workload->fire();
}

void Frontend::run_timer()
{
    TQ q = timers_q.top();
    timers_q.pop();
    now = q.t;
    begin(Cause{'t', q.id});
    timers[q.id].fn(timers[q.id].arg);
}

void Frontend::Impl::add_timer(Frontend *f, AstraSim::timespec_t delta, void (*fn)(void *), void *arg)
{
    int64_t id = (int64_t)f->timers.size();
    Timer t;
    t.delay = (int64_t)delta.time_val;
    t.t = f->now + t.delay;
    t.uid = f->next_uid++;
    t.fn = fn;
    t.arg = arg;
    t.cause = f->cur;
    t.ord = f->ord++;
    f->timers.push_back(t);
    f->timers_q.push(TQ{t.t, t.uid, id});
    f->net->timer_added(id, t);
}

// entry.h's SendFlow; the port is the network's, a QP's tag the flow's own
void Frontend::Impl::send(Frontend *fe, int rank, int dst, uint64_t count, int tag, AstraSim::sim_request *req, void (*h)(void *), void *arg)
{
    sentHash[pk(tag, rank, dst)] = Task{rank, dst, count, arg, h};
    Flow f;
    f.src = rank;
    f.dst = dst;
    f.size = count ? count : 1;
    f.cause = fe->cur;
    f.ord = fe->ord++;
    f.t = fe->now;
    waiting_to_sent_callback[pk(req->flowTag.current_flow_id, rank, dst)]++;
    waiting_to_notify_receiver[pk(req->flowTag.current_flow_id, rank, dst)]++;
    int64_t id = (int64_t)fe->flows.size();
    fe->flows.push_back(f);
    flow_tags.push_back(req->flowTag);
    fe->net->flow_added(id, f);
}

// AstraSimNetwork.cc's sim_recv
void Frontend::Impl::recv(int rank, int src, uint64_t count, void (*h)(void *), void *arg)
{
    Task t{src, rank, count, arg, h};
    auto *ehd = (AstraSim::RecvPacketEventHadndlerData *)arg;
    int tag = ehd->flowTag.tag_id;
    PK k = pk(tag, src, rank);
    auto it = recvHash.find(k);
    if (it == recvHash.end())
    {
        expeRecvHash.emplace(k, t); // (as ns-3's frontend: not over one registered before)
        return;
    }
    uint64_t have = it->second;
    if (have >= t.count)
    {
        if (have == t.count)
            recvHash.erase(it);
        else
            it->second = have - t.count;
        assert(ehd->flowTag.child_flow_id == -1 && ehd->flowTag.current_flow_id == -1);
        auto pq = std::make_pair(std::make_pair(rank, src), tag);
        auto p = receiver_pending_queue.find(pq);
        if (p != receiver_pending_queue.end())
        {
            ehd->flowTag = p->second;
            receiver_pending_queue.erase(p);
        }
        t.msg_handler(t.fun_arg);
    }
    else
    {
        recvHash.erase(it);
        t.count -= have;
        expeRecvHash[k] = t;
    }
}

// entry.h's notify_receiver_receive_data
void Frontend::Impl::notify_receiver(int sender, int receiver, uint64_t size, const AstraSim::ncclFlowTag &tag)
{
    int t = tag.tag_id;
    PK k = pk(t, sender, receiver);
    auto it = expeRecvHash.find(k);
    if (it != expeRecvHash.end())
    {
        Task t2 = it->second;
        auto *ehd = (AstraSim::RecvPacketEventHadndlerData *)t2.fun_arg;
        if (size == t2.count)
        {
            expeRecvHash.erase(it);
            assert(ehd->flowTag.current_flow_id == -1 && ehd->flowTag.child_flow_id == -1);
            ehd->flowTag = tag;
            t2.msg_handler(t2.fun_arg);
        }
        else if (size > t2.count)
        {
            recvHash[k] = size - t2.count;
            expeRecvHash.erase(it);
            assert(ehd->flowTag.current_flow_id == -1 && ehd->flowTag.child_flow_id == -1);
            ehd->flowTag = tag;
            t2.msg_handler(t2.fun_arg);
        }
        else
        {
            t2.count -= size;
            it->second = t2;
        }
    }
    else
    {
        receiver_pending_queue[std::make_pair(std::make_pair(receiver, sender), t)] = tag;
        recvHash[k] += size;
    }
}

// entry.h's notify_sender_sending_finished
void Frontend::Impl::notify_sender(int sender, int receiver, uint64_t size, const AstraSim::ncclFlowTag &tag)
{
    PK k = pk(tag.tag_id, sender, receiver);
    auto it = sentHash.find(k);
    if (it == sentHash.end())
        return;
    Task t2 = it->second;
    auto *ehd = (AstraSim::SendPacketEventHandlerData *)t2.fun_arg;
    ehd->flowTag = tag;
    if (t2.count == size)
    {
        sentHash.erase(it);
        t2.msg_handler(t2.fun_arg);
    }
}

// entry.h's qp_finish
void Frontend::qp_finish(int64_t id)
{
    begin(Cause{'r', id});
    const Flow f = flows[id];
    const AstraSim::ncclFlowTag tag = x->flow_tags[id];
    PK k = pk(tag.current_flow_id, f.src, f.dst);
    x->received_chunksize[k] += f.size;
    auto w = x->waiting_to_notify_receiver.find(k);
    if (w == x->waiting_to_notify_receiver.end() || --w->second != 0)
        return;
    x->waiting_to_notify_receiver.erase(w);
    uint64_t size = x->received_chunksize[k];
    x->received_chunksize.erase(k);
    x->notify_receiver(f.src, f.dst, size, tag);
}

// entry.h's send_finish
void Frontend::send_finish(int64_t id)
{
    begin(Cause{'s', id});
    const Flow f = flows[id];
    const AstraSim::ncclFlowTag tag = x->flow_tags[id];
    PK k = pk(tag.current_flow_id, f.src, f.dst);
    x->sent_chunksize[k] += f.size;
    auto w = x->waiting_to_sent_callback.find(k);
    if (w == x->waiting_to_sent_callback.end() || --w->second != 0)
        return;
    x->waiting_to_sent_callback.erase(w);
    uint64_t size = x->sent_chunksize[k];
    x->sent_chunksize.erase(k);
    x->notify_sender(f.src, f.dst, size, tag);
}

namespace
{
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
    void flow_added(int64_t id, const Flow &f) override
    {
        int64_t tx = 3000 + (int64_t)((double)f.size * 8 / gbps) + 1000;
        q.push(Ev{f.t + tx, seq++, id, false});
        q.push(Ev{f.t + tx + 1000, seq++, id, true});
    }
};
} // namespace

StubStats stub_run(const std::string &topo, const std::string &workload, double gbps, std::vector<Flow> *flows)
{
    Stub net;
    net.gbps = gbps;
    auto t0 = std::chrono::steady_clock::now();
    Frontend fe(topo, workload, &net);
    fe.fire();
    auto t1 = std::chrono::steady_clock::now();
    while (!fe.finished && (fe.has_timer() || !net.q.empty()))
    {
        if (fe.has_timer() && (net.q.empty() || fe.next_timer() <= net.q.top().t))
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
    *flows = fe.flows;
    return StubStats{fe.timers.size(), fe.callbacks, fe.now, fe.finished, std::chrono::duration<double>(t1 - t0).count(), std::chrono::duration<double>(t2 - t1).count()};
}

} // namespace astra
