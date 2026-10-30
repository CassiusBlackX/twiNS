// ns-3's flow hashes, shared by the TCP model (ECMP: twins-ref.cc's per-flow hash) and twins-rdma
// (queue pairs' device picks).
#pragma once
#include <cstdint>
#include "gpu_runtime.h"

// ns-3's Hash32: MurmurHash3_x86_32 with seed 0x8BADF00D (hash-murmur3.cc)
__host__ __device__ __forceinline__ uint32_t ns3_hash32(const uint8_t *key, int len)
{
    const uint32_t c1 = 0xcc9e2d51, c2 = 0x1b873593;
    uint32_t h = 0x8BADF00D;
    int nblocks = len / 4;
    for (int i = 0; i < nblocks; i++)
    {
        uint32_t k = (uint32_t)key[4 * i] | (uint32_t)key[4 * i + 1] << 8 | (uint32_t)key[4 * i + 2] << 16 | (uint32_t)key[4 * i + 3] << 24;
        k *= c1;
        k = (k << 15) | (k >> 17);
        k *= c2;
        h ^= k;
        h = (h << 13) | (h >> 19);
        h = h * 5 + 0xe6546b64;
    }
    const uint8_t *tail = key + 4 * nblocks;
    uint32_t k = 0;
    switch (len & 3)
    {
    case 3:
        k ^= tail[2] << 16;
        [[fallthrough]];
    case 2:
        k ^= tail[1] << 8;
        [[fallthrough]];
    case 1:
        k ^= tail[0];
        k *= c1;
        k = (k << 15) | (k >> 17);
        k *= c2;
        h ^= k;
    }
    h ^= (uint32_t)len;
    h ^= h >> 16;
    h *= 0x85ebca6b;
    h ^= h >> 13;
    h *= 0xc2b2ae35;
    h ^= h >> 16;
    return h;
}
