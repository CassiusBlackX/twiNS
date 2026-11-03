// twins-ai and its twins-islands (src/rdma/island.h): one shared-memory segment per island, in
// /dev/shm. twins-ai writes items into in[] and a command; the island answers in out[]. Items are
// NvlRec, as twins-ai --nvl-record writes them:
//   tag 0: a tail node (n: its code, cnt, next and pos, in the sender's numbering)
//   tag 1: a completion (h, c), island to twins-ai
//   tag 2: a message starting a flow (h.t its time, h.flow the flow, avail its bucket's slot, c)
//   tag 3: a flow ready at the start (main's: h.t, h.flow, c)
//   tag 4: an alias: the receiver's node n.next is the sender's n.pos (a node that came from the
//          receiver: no copy goes back)
// A tag 0 comes before the items whose chains reach it, and after the older node it points to.
#pragma once
#include <atomic>
#include <cstdint>
#include <cstring>
#include <fcntl.h>
#include <linux/futex.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>

struct NvlRec
{
    uint32_t tag, pad;
    int64_t avail;
    CEvH h;
    Chain c;
    TailNode n;
};

enum : uint32_t
{
    ISL_LOAD = 1, // take in[0, in_n), no more
    ISL_RUN = 2,  // take in[0, in_n), then run to limit, then send what it made
    ISL_MORE = 3, // twins-ai has read out[]: send the rest
    ISL_QUIT = 4, // write the flows' completion times (--out) and exit
};

struct IslShm
{
    std::atomic<uint32_t> cmd_seq, done_seq;
    uint32_t cmd, more; // more: out[] holds part of what the island made; ISL_MORE for the rest
    int64_t limit;      // ISL_RUN: the slot to run to
    int64_t slot;       // the island's slot after the command
    uint64_t in_n, out_n, in_cap, out_cap;
    double busy_s; // the island's time in its runs
    NvlRec *in() { return reinterpret_cast<NvlRec *>(this + 1); }
    NvlRec *out() { return in() + in_cap; }
    static size_t bytes(uint64_t in_cap, uint64_t out_cap) { return sizeof(IslShm) + (in_cap + out_cap) * sizeof(NvlRec); }
};

inline void isl_wait(std::atomic<uint32_t> &w, uint32_t old)
{
    while (w.load(std::memory_order_acquire) == old)
        syscall(SYS_futex, reinterpret_cast<uint32_t *>(&w), FUTEX_WAIT, old, nullptr, nullptr, 0);
}
inline void isl_wake(std::atomic<uint32_t> &w)
{
    w.fetch_add(1, std::memory_order_release);
    syscall(SYS_futex, reinterpret_cast<uint32_t *>(&w), FUTEX_WAKE, 1 << 30, nullptr, nullptr, 0);
}
// a segment, created (twins-ai) or opened (the island)
inline IslShm *isl_map(const char *name, bool create, uint64_t in_cap = 0, uint64_t out_cap = 0)
{
    int fd = shm_open(name, create ? O_CREAT | O_RDWR | O_TRUNC : O_RDWR, 0600);
    if (fd < 0)
        return nullptr;
    size_t n;
    if (create)
    {
        n = IslShm::bytes(in_cap, out_cap);
        if (ftruncate(fd, n))
            return nullptr;
    }
    else
    {
        IslShm h;
        if (pread(fd, &h, sizeof(h), 0) != (ssize_t)sizeof(h))
            return nullptr;
        n = IslShm::bytes(h.in_cap, h.out_cap);
    }
    void *p = mmap(nullptr, n, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (p == MAP_FAILED)
        return nullptr;
    IslShm *s = static_cast<IslShm *>(p);
    if (create)
    {
        memset((void *)s, 0, sizeof(IslShm));
        s->in_cap = in_cap;
        s->out_cap = out_cap;
    }
    return s;
}
