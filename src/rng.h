// ns-3's random number generator, MRG32k3a (RngStream), for the per-packet ECMP choice of
// Ipv4GlobalRouting (UniformRandomVariable::GetInteger). The state is advanced exactly as ns-3
// does, in doubles (every intermediate value is an integer below 2^53, so the results do not
// depend on FMA contraction); a stream's initial state is computed on the host with integer
// matrix powers: RngStream(seed, stream, substream) = A^(stream 2^127 + substream 2^76) seed,
// where a deterministic stream s (SetStream(s)) is stream 2^63 + s and the substream is the run.
#ifndef TWINS_RNG_H
#define TWINS_RNG_H

#include <cstdint>

#if defined(__HIPCC__) || defined(__CUDACC__)
#define RNG_FN __host__ __device__ inline
#else
#define RNG_FN inline
#endif

namespace nsm_rng
{
constexpr double M1 = 4294967087.0, M2 = 4294944443.0, NORM = 1.0 / (4294967087.0 + 1.0);
constexpr double A12 = 1403580.0, A13N = 810728.0, A21 = 527612.0, A23N = 1370589.0;

struct State
{
    double s[6];
};

// RngStream::RandU01
RNG_FN double rand_u01(State &x)
{
    double *s = x.s;
    double p1 = A12 * s[1] - A13N * s[0];
    int32_t k = (int32_t)(p1 / M1);
    p1 -= k * M1;
    if (p1 < 0.0)
        p1 += M1;
    s[0] = s[1];
    s[1] = s[2];
    s[2] = p1;
    double p2 = A21 * s[5] - A23N * s[3];
    k = (int32_t)(p2 / M2);
    p2 -= k * M2;
    if (p2 < 0.0)
        p2 += M2;
    s[3] = s[4];
    s[4] = s[5];
    s[5] = p2;
    return (p1 > p2) ? (p1 - p2) * NORM : (p1 - p2 + M1) * NORM;
}

// UniformRandomVariable::GetInteger(0, n - 1) = (uint32_t)(0 + u * (n - 0))
RNG_FN uint32_t get_integer(State &x, uint32_t n)
{
    double u = rand_u01(x);
    return (uint32_t)(0.0 + u * ((double)(n - 1) + 1.0 - 0.0));
}

// ---- host: initial states
typedef uint64_t Mat[3][3];

inline uint64_t mulmod(uint64_t a, uint64_t b, uint64_t m) { return (uint64_t)((unsigned __int128)a * b % m); }

inline void mat_mul(const Mat a, const Mat b, Mat out, uint64_t m)
{
    Mat r;
    for (int i = 0; i < 3; i++)
        for (int j = 0; j < 3; j++)
        {
            unsigned __int128 v = 0;
            for (int k = 0; k < 3; k++)
                v += (unsigned __int128)a[i][k] * b[k][j];
            r[i][j] = (uint64_t)(v % m);
        }
    for (int i = 0; i < 3; i++)
        for (int j = 0; j < 3; j++)
            out[i][j] = r[i][j];
}

inline void mat_vec(const Mat a, uint64_t v[3], uint64_t m)
{
    uint64_t r[3];
    for (int i = 0; i < 3; i++)
    {
        unsigned __int128 x = 0;
        for (int k = 0; k < 3; k++)
            x += (unsigned __int128)a[i][k] * v[k];
        r[i] = (uint64_t)(x % m);
    }
    for (int i = 0; i < 3; i++)
        v[i] = r[i];
}

// v = A^(nth 2^by) v (RngStream::AdvanceNthBy)
inline void advance(const Mat a, uint64_t m, uint64_t nth, int by, uint64_t v[3])
{
    Mat p; // A^(2^by)
    for (int i = 0; i < 3; i++)
        for (int j = 0; j < 3; j++)
            p[i][j] = a[i][j];
    for (int i = 0; i < by; i++)
        mat_mul(p, p, p, m);
    for (int bit = 0; bit < 64; bit++)
    {
        if ((nth >> bit) & 1)
            mat_vec(p, v, m);
        mat_mul(p, p, p, m);
    }
}

// advance() with the powers A^(2^(by + bit)) computed once (twins-rdma sets one stream per switch
// port: tens of thousands at k=32)
struct Powers
{
    Mat p[64];
    Powers(const Mat a, uint64_t m, int by)
    {
        Mat x;
        for (int i = 0; i < 3; i++)
            for (int j = 0; j < 3; j++)
                x[i][j] = a[i][j];
        for (int i = 0; i < by; i++)
            mat_mul(x, x, x, m);
        for (int bit = 0; bit < 64; bit++)
        {
            for (int i = 0; i < 3; i++)
                for (int j = 0; j < 3; j++)
                    p[bit][i][j] = x[i][j];
            mat_mul(x, x, x, m);
        }
    }
    void advance(uint64_t m, uint64_t nth, uint64_t v[3]) const
    {
        for (int bit = 0; bit < 64; bit++)
            if ((nth >> bit) & 1)
                mat_vec(p[bit], v, m);
    }
};

// the state of UniformRandomVariable::SetStream(stream) under RngSeed seed, RngRun run
inline State stream_state(uint32_t seed, uint64_t stream, uint64_t run)
{
    const uint64_t m1 = 4294967087ull, m2 = 4294944443ull;
    const Mat A1 = {{0, 1, 0}, {0, 0, 1}, {m1 - 810728, 1403580, 0}};
    const Mat A2 = {{0, 1, 0}, {0, 0, 1}, {m2 - 1370589, 0, 527612}};
    static const Powers s1(A1, m1, 127), s2(A2, m2, 127), r1(A1, m1, 76), r2(A2, m2, 76);
    uint64_t v1[3] = {seed, seed, seed}, v2[3] = {seed, seed, seed};
    uint64_t target = (1ull << 63) + stream;
    s1.advance(m1, target, v1);
    s2.advance(m2, target, v2);
    r1.advance(m1, run, v1);
    r2.advance(m2, run, v2);
    State x;
    for (int i = 0; i < 3; i++)
    {
        x.s[i] = (double)v1[i];
        x.s[3 + i] = (double)v2[i];
    }
    return x;
}
} // namespace nsm_rng

#endif
