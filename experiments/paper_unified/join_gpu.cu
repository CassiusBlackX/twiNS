// Exercise the exact comparator used by the live closed coordinator for equal leads.
#include <cstdlib>
#include <iostream>
#define TWINS_ASTRA
#include "rdma/rstep.cuh"
using namespace rdma;

#define CHECK(call) do { auto e = (call); if (e != hipSuccess) { std::cerr << hipGetErrorString(e) << '\n'; return 1; } } while (0)

__global__ void setup(Chain *timer, Chain *receive, bool timer_first)
{
    Chain root{};
    root.root = 1;
    cpush(timer, ccode(25, timer_first ? 0 : 1), root);
    cpush(receive, ccode(25, timer_first ? 1 : 0), root);
}

int main()
{
    Sim s{};
    Chain *timer = nullptr, *receive = nullptr;
    CHECK(hipMalloc((void **)&timer,sizeof(*timer)));
    CHECK(hipMalloc((void **)&receive,sizeof(*receive)));
    CHECK(hipMalloc((void **)&s.cl,sizeof(ClCtl)));
    CHECK(hipMalloc((void **)&s.ev,40*sizeof(unsigned long long)));
    CHECK(hipMemset(s.ev,0,40*sizeof(unsigned long long)));
    s.tchain=timer;
    s.cev_c=receive;
    const uint64_t base[2]={1000003,1000033};
    CHECK(hipMemcpyToSymbol(HIP_SYMBOL(c_fp),base,sizeof(base)));
    CHECK(hipMemcpyToSymbol(HIP_SYMBOL(c_sim),&s,sizeof(s)));
    for (bool timer_first : {true,false})
    {
        setup<<<1,1>>>(timer,receive,timer_first);
        closed_cmp<<<1,1>>>(0,0);
        int cmp=0;
        CHECK(hipMemcpy(&cmp,&s.cl->cmp,sizeof(cmp),hipMemcpyDeviceToHost));
        if (!cmp) return 2;
        std::cout << int(timer_first) << ' ' << (cmp <= 0 ? 't' : 'r') << " 25\n";
        std::cout << int(timer_first) << ' ' << (cmp <= 0 ? 'r' : 't') << " 25\n";
        std::cout << int(timer_first) << " send 26\n";
    }
}
