// ns-3's Hash64 (hash-murmur3.cc: MurmurHash3_x86_128, all four seeds 0x8BADF00D, result
// hash[1] << 32 | hash[0]), used by ns-3-ub for ECMP (UbRoutingProcess HASH64) and phase hashes.
#pragma once
#include <cstdint>
#include <cstddef>
#ifndef UB_HD
#if defined(__CUDACC__) || defined(__HIPCC__)
#define UB_HD __host__ __device__
#else
#define UB_HD
#endif
#endif

namespace ub {

UB_HD inline uint32_t rotl32(uint32_t x, int r) { return (x << r) | (x >> (32 - r)); }
UB_HD inline uint32_t fmix32(uint32_t h)
{
    h ^= h >> 16;
    h *= 0x85ebca6b;
    h ^= h >> 13;
    h *= 0xc2b2ae35;
    h ^= h >> 16;
    return h;
}

UB_HD inline uint64_t ns3_hash64(const uint8_t *data, size_t len)
{
    const uint32_t S = 0x8BADF00D;
    uint32_t h1 = S, h2 = S, h3 = S, h4 = S;
    const uint32_t c1 = 0x239b961b, c2 = 0xab0e9789, c3 = 0x38b34ae5, c4 = 0xa1e38b93;
    size_t nblocks = len / 16;
    auto blk = [&](size_t i) {
        const uint8_t *p = data + 4 * i;
        return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
    };
    for (size_t i = 0; i < nblocks; i++) {
        uint32_t k1 = blk(i * 4 + 0), k2 = blk(i * 4 + 1), k3 = blk(i * 4 + 2), k4 = blk(i * 4 + 3);
        k1 *= c1; k1 = rotl32(k1, 15); k1 *= c2; h1 ^= k1;
        h1 = rotl32(h1, 19); h1 += h2; h1 = h1 * 5 + 0x561ccd1b;
        k2 *= c2; k2 = rotl32(k2, 16); k2 *= c3; h2 ^= k2;
        h2 = rotl32(h2, 17); h2 += h3; h2 = h2 * 5 + 0x0bcaa747;
        k3 *= c3; k3 = rotl32(k3, 17); k3 *= c4; h3 ^= k3;
        h3 = rotl32(h3, 15); h3 += h4; h3 = h3 * 5 + 0x96cd1c35;
        k4 *= c4; k4 = rotl32(k4, 18); k4 *= c1; h4 ^= k4;
        h4 = rotl32(h4, 13); h4 += h1; h4 = h4 * 5 + 0x32ac3b17;
    }
    const uint8_t *tail = data + nblocks * 16;
    uint32_t k1 = 0, k2 = 0, k3 = 0, k4 = 0;
    switch (len & 15) {
    case 15: k4 ^= (uint32_t)tail[14] << 16; [[fallthrough]];
    case 14: k4 ^= (uint32_t)tail[13] << 8; [[fallthrough]];
    case 13: k4 ^= (uint32_t)tail[12];
        k4 *= c4; k4 = rotl32(k4, 18); k4 *= c1; h4 ^= k4; [[fallthrough]];
    case 12: k3 ^= (uint32_t)tail[11] << 24; [[fallthrough]];
    case 11: k3 ^= (uint32_t)tail[10] << 16; [[fallthrough]];
    case 10: k3 ^= (uint32_t)tail[9] << 8; [[fallthrough]];
    case 9: k3 ^= (uint32_t)tail[8];
        k3 *= c3; k3 = rotl32(k3, 17); k3 *= c4; h3 ^= k3; [[fallthrough]];
    case 8: k2 ^= (uint32_t)tail[7] << 24; [[fallthrough]];
    case 7: k2 ^= (uint32_t)tail[6] << 16; [[fallthrough]];
    case 6: k2 ^= (uint32_t)tail[5] << 8; [[fallthrough]];
    case 5: k2 ^= (uint32_t)tail[4];
        k2 *= c2; k2 = rotl32(k2, 16); k2 *= c3; h2 ^= k2; [[fallthrough]];
    case 4: k1 ^= (uint32_t)tail[3] << 24; [[fallthrough]];
    case 3: k1 ^= (uint32_t)tail[2] << 16; [[fallthrough]];
    case 2: k1 ^= (uint32_t)tail[1] << 8; [[fallthrough]];
    case 1: k1 ^= (uint32_t)tail[0];
        k1 *= c1; k1 = rotl32(k1, 15); k1 *= c2; h1 ^= k1;
    }
    uint32_t L = (uint32_t)len;
    h1 ^= L; h2 ^= L; h3 ^= L; h4 ^= L;
    h1 += h2; h1 += h3; h1 += h4;
    h2 += h1; h3 += h1; h4 += h1;
    h1 = fmix32(h1); h2 = fmix32(h2); h3 = fmix32(h3); h4 = fmix32(h4);
    h1 += h2; h1 += h3; h1 += h4;
    h2 += h1; h3 += h1; h4 += h1;
    return (uint64_t)h2 << 32 | h1;
}

} // namespace ub
