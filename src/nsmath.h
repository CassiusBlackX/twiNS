// Bit-exact replicas of the floating-point functions ns-3's TCP models use on x86-64, for host
// and GPU code:
//   nsm_pow      glibc 2.39 pow, the FMA variant (__ieee754_pow_fma) that glibc's IFUNC selects
//                on CPUs with FMA and AVX2 (sysdeps/ieee754/dbl-64/e_pow.c, __FP_FAST_FMA). That
//                variant is built by GCC with -mfma, which also fuses every multiplication whose
//                result only feeds additions; those fusions are written out as fma() here.
//   nsm_seconds  ns3::Time::GetSeconds() for a time in picoseconds: Q64.64 multiplication by the
//                inverse of 10^12 (int64x64-128), then GetDouble()'s x87 long double sum
//                rounded to double
// Callers must not let the compiler contract other expressions into FMAs (-ffp-contract=off).
#ifndef TWINS_NSMATH_H
#define TWINS_NSMATH_H

#include <cstdint>
#include <cstring>

#if defined(__HIPCC__) || defined(__CUDACC__)
#define NSM_FN __host__ __device__ inline
#else
#define NSM_FN inline
#endif

#if defined(__HIP_DEVICE_COMPILE__) || defined(__CUDA_ARCH__)
#define NSM_TABLE static __constant__ const
#else
#define NSM_TABLE static const
#endif

namespace nsm_math
{
#include "nsmath_tables.inc"

NSM_FN uint64_t asuint64(double f)
{
    uint64_t i;
    memcpy(&i, &f, 8);
    return i;
}

NSM_FN double asdouble(uint64_t i)
{
    double f;
    memcpy(&f, &i, 8);
    return f;
}

NSM_FN uint32_t top12(double x)
{
    return asuint64(x) >> 52;
}

// log(x) as hi + tail (e_pow.c log_inline, __FP_FAST_FMA path)
NSM_FN double log_inline(uint64_t ix, double *tail)
{
    const int N = 128;
    const uint64_t OFF = 0x3fe6955500000000ull;
    uint64_t tmp = ix - OFF;
    int i = (int)((tmp >> (52 - 7)) % N);
    int k = (int)((int64_t)tmp >> 52);
    uint64_t iz = ix - (tmp & 0xfffull << 52);
    double z = asdouble(iz);
    double kd = (double)k;
    double invc = nsm_pow_tab[i][0], logc = nsm_pow_tab[i][1], logctail = nsm_pow_tab[i][2];
    double r = __builtin_fma(z, invc, -1.0);
    double t1 = __builtin_fma(kd, nsm_pow_ln2hi, logc);
    double t2 = t1 + r;
    double lo1 = __builtin_fma(kd, nsm_pow_ln2lo, logctail);
    double lo2 = t1 - t2 + r;
    const double *A = nsm_pow_poly;
    double ar = A[0] * r;
    double ar2 = r * ar;
    double ar3 = r * ar2;
    double hi = t2 + ar2;
    double lo3 = __builtin_fma(ar, r, -ar2);
    double lo4 = t2 - hi + ar2;
    double q = __builtin_fma(ar2, __builtin_fma(r, A[6], A[5]), __builtin_fma(r, A[4], A[3]));
    q = __builtin_fma(ar2, q, __builtin_fma(r, A[2], A[1]));
    double lo = __builtin_fma(ar3, q, lo1 + lo2 + lo3 + lo4); // + p, p = ar3 * q
    double y = hi + lo;
    *tail = hi - y + lo;
    return y;
}

NSM_FN double specialcase(double tmp, uint64_t sbits, uint64_t ki)
{
    double scale, y;
    if ((ki & 0x80000000) == 0)
    {
        sbits -= 1009ull << 52;
        scale = asdouble(sbits);
        return 0x1p1009 * __builtin_fma(scale, tmp, scale);
    }
    sbits += 1022ull << 52;
    scale = asdouble(sbits);
    y = __builtin_fma(scale, tmp, scale);
    if ((y < 0 ? -y : y) < 1.0)
    {
        double hi, lo, one = 1.0;
        if (y < 0.0)
            one = -1.0;
        lo = __builtin_fma(scale, tmp, scale - y);
        hi = one + y;
        lo = one - hi + y + lo;
        y = (hi + lo) - one;
        if (y == 0.0)
            y = asdouble(sbits & 0x8000000000000000ull);
    }
    return 0x1p-1022 * y;
}

// sign * exp(x + xtail) (e_pow.c exp_inline; TOINT_INTRINSICS 0)
NSM_FN double exp_inline(double x, double xtail, uint32_t sign_bias)
{
    const int N = 128;
    uint32_t abstop = top12(x) & 0x7ff;
    if (abstop - top12(0x1p-54) >= top12(512.0) - top12(0x1p-54))
    {
        if (abstop - top12(0x1p-54) >= 0x80000000)
        {
            double one = 1.0 + x;
            return sign_bias ? -one : one;
        }
        if (abstop >= top12(1024.0))
        {
            double inf = asdouble(0x7ff0000000000000ull), zero = 0.0;
            if (asuint64(x) >> 63)
                return sign_bias ? -zero : zero;
            return sign_bias ? -inf : inf;
        }
        abstop = 0;
    }
    double kd = __builtin_fma(nsm_exp_invln2N, x, nsm_exp_shift); // z = InvLn2N * x; z + Shift
    uint64_t ki = asuint64(kd);
    kd -= nsm_exp_shift;
    double r = __builtin_fma(kd, nsm_exp_negln2loN, __builtin_fma(kd, nsm_exp_negln2hiN, x));
    r += xtail;
    uint64_t idx = 2 * (ki % N);
    uint64_t top = (ki + sign_bias) << (52 - 7);
    double tail = asdouble(nsm_exp_tab[idx]);
    uint64_t sbits = nsm_exp_tab[idx + 1] + top;
    double r2 = r * r;
    const double *P = nsm_exp_poly;
    double tmp = __builtin_fma(r2 * r2, __builtin_fma(r, P[3], P[2]), __builtin_fma(r2, __builtin_fma(r, P[1], P[0]), tail + r));
    if (abstop == 0)
        return specialcase(tmp, sbits, ki);
    double scale = asdouble(sbits);
    return __builtin_fma(scale, tmp, scale);
}

NSM_FN int checkint(uint64_t iy)
{
    int e = iy >> 52 & 0x7ff;
    if (e < 0x3ff)
        return 0;
    if (e > 0x3ff + 52)
        return 2;
    if (iy & ((1ull << (0x3ff + 52 - e)) - 1))
        return 0;
    if (iy & (1ull << (0x3ff + 52 - e)))
        return 1;
    return 2;
}

NSM_FN int zeroinfnan(uint64_t i)
{
    return 2 * i - 1 >= 2 * 0x7ff0000000000000ull - 1;
}
} // namespace nsm_math

NSM_FN double nsm_pow(double x, double y)
{
    using namespace nsm_math;
    const uint32_t SIGN_BIAS = 0x800 << 7;
    uint32_t sign_bias = 0;
    uint64_t ix = asuint64(x), iy = asuint64(y);
    uint32_t topx = top12(x), topy = top12(y);
    const uint64_t ONE = 0x3ff0000000000000ull, INF = 0x7ff0000000000000ull;
    if (topx - 0x001 >= 0x7ff - 0x001 || (topy & 0x7ff) - 0x3be >= 0x43e - 0x3be)
    {
        if (zeroinfnan(iy))
        {
            if (2 * iy == 0)
                return 1.0;
            if (ix == ONE)
                return 1.0;
            if (2 * ix > 2 * INF || 2 * iy > 2 * INF)
                return x + y;
            if (2 * ix == 2 * ONE)
                return 1.0;
            if ((2 * ix < 2 * ONE) == !(iy >> 63))
                return 0.0;
            return y * y;
        }
        if (zeroinfnan(ix))
        {
            double x2 = x * x;
            if (ix >> 63 && checkint(iy) == 1)
                x2 = -x2;
            return iy >> 63 ? 1 / x2 : x2;
        }
        if (ix >> 63)
        {
            int yint = checkint(iy);
            if (yint == 0)
                return asdouble(0x7ff8000000000000ull);
            if (yint == 1)
                sign_bias = SIGN_BIAS;
            ix &= 0x7fffffffffffffffull;
            topx &= 0x7ff;
        }
        if ((topy & 0x7ff) - 0x3be >= 0x43e - 0x3be)
        {
            if (ix == ONE)
                return 1.0;
            if ((topy & 0x7ff) < 0x3be)
                return ix > ONE ? 1.0 + y : 1.0 - y;
            return (ix > ONE) == (topy < 0x800) ? asdouble(INF) : 0.0;
        }
        if (topx == 0)
        {
            ix = asuint64(x * 0x1p52);
            ix &= 0x7fffffffffffffffull;
            ix -= 52ull << 52;
        }
    }
    double lo;
    double hi = log_inline(ix, &lo);
    double ehi = y * hi;
    double elo = __builtin_fma(y, lo, __builtin_fma(y, hi, -ehi));
    return exp_inline(ehi, elo, sign_bias);
}

// ns3::Time(ps).GetSeconds() with Time resolution PS. inv_hi/inv_lo: the Q64.64 value of
// int64x64_t::Invert(10^12) (constants checked against ns-3, see validation/tools/nsmath_ref.cc).
constexpr uint64_t NSM_INV_1E12_HI = 18446744ull, NSM_INV_1E12_LO = 1359701234448236304ull;

NSM_FN double nsm_seconds(int64_t ps)
{
    if (ps == 0)
        return 0.0;
    bool neg = ps < 0;
    unsigned __int128 a = neg ? (unsigned __int128)(-(__int128)ps) : (unsigned __int128)ps; // = ah (al = 0)
    // UmulByInvert: ah * bh + (ah * bl) >> 64, with bh = NSM_INV_1E12_HI, bl = NSM_INV_1E12_LO
    unsigned __int128 v = a * NSM_INV_1E12_HI + ((a * NSM_INV_1E12_LO) >> 64);
    // GetDouble: fhi + flo in x87 extended precision (64-bit significand), then to double
    if (v == 0)
        return neg ? -0.0 : 0.0;
    int msb = 127;
    while (!((v >> msb) & 1))
        msb--;
    // round v to 64 significant bits (nearest, ties to even) unless it already fits
    if (msb > 63)
    {
        int drop = msb - 63;
        unsigned __int128 half = (unsigned __int128)1 << (drop - 1);
        unsigned __int128 rem = v & (((unsigned __int128)1 << drop) - 1);
        v >>= drop;
        if (rem > half || (rem == half && (v & 1)))
            v += 1;
        v <<= drop;
        msb = 127;
        while (!((v >> msb) & 1))
            msb--;
    }
    // round to 53 significant bits (nearest, ties to even)
    if (msb > 52)
    {
        int drop = msb - 52;
        unsigned __int128 half = (unsigned __int128)1 << (drop - 1);
        unsigned __int128 rem = v & (((unsigned __int128)1 << drop) - 1);
        v >>= drop;
        if (rem > half || (rem == half && (v & 1)))
            v += 1;
        v <<= drop;
        msb = 127;
        while (!((v >> msb) & 1))
            msb--;
    }
    // value = v * 2^-64 with at most 53 significant bits: convert exactly
    uint64_t mant = (uint64_t)(v >> (msb > 52 ? msb - 52 : 0));
    int e2 = (msb > 52 ? msb - 52 : 0) - 64;
    double d = (double)mant; // exact (< 2^53)
    while (e2 > 0)
    {
        d *= 2.0;
        e2--;
    }
    while (e2 < 0)
    {
        int s = e2 < -60 ? 60 : -e2;
        d *= nsm_math::asdouble((uint64_t)(1023 - s) << 52);
        e2 += s;
    }
    return neg ? -d : d;
}

#endif
