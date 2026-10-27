// Checks src/nsmath.h against validation/tools/nsmath_ref.cc output, on the host and (when
// built with hipcc/nvcc) on the GPU.  usage: nsmath_check < nsmath_ref.txt
#include <cstdio>
#include <cstring>
#include <vector>
#if defined(__HIPCC__)
#include <hip/hip_runtime.h>
#endif
#include "nsmath.h"

static double bd(unsigned long long u) { double d; memcpy(&d, &u, 8); return d; }
static unsigned long long db(double d) { unsigned long long u; memcpy(&u, &d, 8); return u; }

#if defined(__HIPCC__) || defined(__CUDACC__)
__global__ void kern(const long long *ps, int ns, double *so, const double *x, const double *y, int np, double *po)
{
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < ns)
        so[i] = nsm_seconds(ps[i]);
    if (i < np)
        po[i] = nsm_pow(x[i], y[i]);
}
#endif

int main()
{
    std::vector<long long> ps;
    std::vector<unsigned long long> sref, pref;
    std::vector<double> x, y;
    char kind[8];
    while (scanf("%7s", kind) == 1)
    {
        if (!strcmp(kind, "inv"))
        {
            long long hi;
            unsigned long long lo;
            scanf("%lld %llu", &hi, &lo);
            printf("inv %s\n", (unsigned long long)hi == NSM_INV_1E12_HI && lo == NSM_INV_1E12_LO ? "matches" : "DIFFERS");
        }
        else if (!strcmp(kind, "sec"))
        {
            long long p;
            unsigned long long b;
            scanf("%lld %llu", &p, &b);
            ps.push_back(p);
            sref.push_back(b);
        }
        else
        {
            unsigned long long a, c, b;
            scanf("%llu %llu %llu", &a, &c, &b);
            x.push_back(bd(a));
            y.push_back(bd(c));
            pref.push_back(b);
        }
    }
    long bad_s = 0, bad_p = 0;
    for (size_t i = 0; i < ps.size(); i++)
        bad_s += db(nsm_seconds(ps[i])) != sref[i];
    for (size_t i = 0; i < x.size(); i++)
        if (db(nsm_pow(x[i], y[i])) != pref[i] && bad_p++ < 3)
            printf("host pow(%a, %a) = %a, ref %a\n", x[i], y[i], nsm_pow(x[i], y[i]), bd(pref[i]));
    printf("host: seconds %ld/%zu differ, pow %ld/%zu differ\n", bad_s, ps.size(), bad_p, x.size());
#if defined(__HIPCC__) || defined(__CUDACC__)
    long long *dps; double *dso, *dx, *dy, *dpo;
    int ns = ps.size(), np = x.size(), n = ns > np ? ns : np;
    hipMalloc(&dps, 8 * ns); hipMalloc(&dso, 8 * ns); hipMalloc(&dx, 8 * np); hipMalloc(&dy, 8 * np); hipMalloc(&dpo, 8 * np);
    hipMemcpy(dps, ps.data(), 8 * ns, hipMemcpyHostToDevice);
    hipMemcpy(dx, x.data(), 8 * np, hipMemcpyHostToDevice);
    hipMemcpy(dy, y.data(), 8 * np, hipMemcpyHostToDevice);
    kern<<<(n + 255) / 256, 256>>>(dps, ns, dso, dx, dy, np, dpo);
    std::vector<double> so(ns), po(np);
    hipMemcpy(so.data(), dso, 8 * ns, hipMemcpyDeviceToHost);
    hipMemcpy(po.data(), dpo, 8 * np, hipMemcpyDeviceToHost);
    long gs = 0, gp = 0;
    for (int i = 0; i < ns; i++)
        gs += db(so[i]) != sref[i];
    for (int i = 0; i < np; i++)
        if (db(po[i]) != pref[i] && gp++ < 3)
            printf("gpu pow(%a, %a) = %a, ref %a\n", x[i], y[i], po[i], bd(pref[i]));
    printf("gpu: seconds %ld/%d differ, pow %ld/%d differ\n", gs, ns, gp, np);
#endif
    return 0;
}
