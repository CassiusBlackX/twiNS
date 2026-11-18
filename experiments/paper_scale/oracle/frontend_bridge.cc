// Drive the same ASTRA system layer directly from the author's sequential event loop.
#include "astra/frontend.h"
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <stdexcept>
extern "C" void enqueue_flow(int id, int src, int dst, unsigned long long size);
extern "C" void enqueue_timer(int id, long long delay);
namespace {
struct Live : astra::Network {
    std::unique_ptr<astra::Frontend> fe;
    void flow_added(int64_t id, const astra::Flow &f) override {
        enqueue_flow(id,f.src,f.dst,f.size);
    }
    void timer_added(int64_t id, const astra::Timer &t) override {
        enqueue_timer(id,t.delay);
    }
} network;
}
extern "C" void start_frontend() {
    network.fe.reset(new astra::Frontend(std::getenv("AI_TOPO"), std::getenv("AI_WORKLOAD"), &network));
    network.fe->fire();
}
extern "C" bool advance_frontend(int kind,int id,long long now) {
    auto &fe=*network.fe;
    fe.now=now;
    if(kind==0) fe.qp_finish(id);
    else if(kind==1) fe.send_finish(id);
    else {
        if(!fe.has_timer() || fe.next_timer_id()!=id || fe.next_timer()!=now)
            throw std::runtime_error("ns-3 and ASTRA timer order disagree");
        fe.run_timer();
    }
    return fe.finished;
}
extern "C" bool save_frontend() {
    auto &fe=*network.fe;
    FILE *out=std::fopen("reference.dag","w");
    if(!out) throw std::runtime_error("opening reference.dag");
    for(size_t i=0;i<fe.flows.size();i++) {
        const auto &x=fe.flows[i];
        std::fprintf(out,"flow %zu %d %d %llu\n",i,x.src,x.dst,(unsigned long long)x.size);
    }
    for(size_t i=0;i<fe.timers.size();i++) std::fprintf(out,"timer %zu %lld\n",i,(long long)fe.timers[i].delay);
    auto cause=[&](char kind,size_t i,const astra::Cause &c,unsigned ord){
        if(c.kind=='m') std::fprintf(out,"on %c%zu m\n",kind,i);
        else std::fprintf(out,"on %c%zu %c%lld %u\n",kind,i,c.kind,(long long)c.id,ord);
    };
    for(size_t i=0;i<fe.flows.size();i++) cause('f',i,fe.flows[i].cause,fe.flows[i].ord);
    for(size_t i=0;i<fe.timers.size();i++) cause('t',i,fe.timers[i].cause,fe.timers[i].ord);
    std::fclose(out);
    return fe.finished && !fe.has_timer();
}
