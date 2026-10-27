// Checks src/rng.h against validation/tools/rng_ref.cc output (ns-3), on the host and the GPU.
// usage: rng_check <run> < rng_ref.txt
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <vector>
#if defined(__HIPCC__)
#include <hip/hip_runtime.h>
#endif
#include "rng.h"
using namespace nsm_rng;

#if defined(__HIPCC__) || defined(__CUDACC__)
__global__ void kern(State *st, int nst, double *u, uint32_t *v, int per)
{
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= nst)
        return;
    State a = st[i], b = st[i];
    for (int j = 0; j < per; j++)
        u[i * per + j] = rand_u01(a);
    for (int j = 0; j < per; j++)
        v[i * per + j] = get_integer(b, 1 + j % 16);
}
#endif

int main(int argc, char **argv)
{
    uint64_t run = argc > 1 ? atoll(argv[1]) : 1;
    std::vector<long long> streams;
    std::vector<unsigned long long> ub;
    std::vector<unsigned> iv;
    char kind[8];
    long long st;
    while (scanf("%7s %lld", kind, &st) == 2)
    {
        if (streams.empty() || streams.back() != st)
            streams.push_back(st);
        if (!strcmp(kind, "u"))
        {
            int i;
            unsigned long long b;
            scanf("%d %llu", &i, &b);
            ub.push_back(b);
        }
        else
        {
            unsigned n, x;
            int i;
            scanf("%u %d %u", &n, &i, &x);
            iv.push_back(x);
        }
    }
    int ns = (int)streams.size(), per = (int)(ub.size() / ns);
    std::vector<State> s0(ns);
    long bad = 0;
    for (int k = 0; k < ns; k++)
    {
        s0[k] = stream_state(1, streams[k], run);
        State a = s0[k], b = s0[k];
        for (int j = 0; j < per; j++)
        {
            double u = rand_u01(a);
            unsigned long long x;
            memcpy(&x, &u, 8);
            bad += x != ub[k * per + j];
            bad += get_integer(b, 1 + j % 16) != iv[k * per + j];
        }
    }
    printf("host: %d streams x %d draws, %ld differ\n", ns, per, bad);
#if defined(__HIPCC__) || defined(__CUDACC__)
    State *ds;
    double *du;
    uint32_t *dv;
    hipMalloc(&ds, sizeof(State) * ns);
    hipMalloc(&du, sizeof(double) * ns * per);
    hipMalloc(&dv, sizeof(uint32_t) * ns * per);
    hipMemcpy(ds, s0.data(), sizeof(State) * ns, hipMemcpyHostToDevice);
    kern<<<1, 64>>>(ds, ns, du, dv, per);
    std::vector<double> u(ns * per);
    std::vector<uint32_t> v(ns * per);
    hipMemcpy(u.data(), du, sizeof(double) * ns * per, hipMemcpyDeviceToHost);
    hipMemcpy(v.data(), dv, sizeof(uint32_t) * ns * per, hipMemcpyDeviceToHost);
    bad = 0;
    for (int i = 0; i < ns * per; i++)
    {
        unsigned long long x;
        memcpy(&x, &u[i], 8);
        bad += x != ub[i];
        bad += v[i] != iv[i];
    }
    printf("gpu: %ld differ\n", bad);
#endif
}
