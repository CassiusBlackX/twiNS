// Check the production comparator against a sequential (time, insertion UID) scheduler.
#include <algorithm>
#include <array>
#include <iostream>
#include <queue>
#include <tuple>
#include <utility>
#include <vector>
#include "rdma/rstep.cuh"
using namespace rdma;

#define CHECK(call) do { const auto error = (call); if (error != hipSuccess) { \
    std::cerr << #call << ": " << hipGetErrorString(error) << '\n'; std::exit(1); } } while (0)

struct Event
{
    int parent;
    int ordinal;
    int root;
    int64_t time;
};

struct Fixture
{
    const char *label;
    std::vector<Event> tree;
    std::vector<int> inserted, uid;
    int a, b;

    int add(int parent, int64_t delay, int root = 0)
    {
        const int n = tree.size();
        const auto ordinal = std::count_if(tree.begin(), tree.end(), [=](const Event &x) { return x.parent == parent; });
        tree.push_back({parent, int(ordinal), parent < 0 ? root : tree[parent].root,
                        delay + (parent < 0 ? 0 : tree[parent].time)});
        return n;
    }

    int path(int n, int depth)
    {
        for (int j = 0; j < depth; ++j)
            n = add(n, 1 + j % 3);
        return n;
    }

    void schedule()
    {
        using Key = std::tuple<int64_t, int, int>;
        std::priority_queue<Key, std::vector<Key>, std::greater<Key>> queue;
        uid.resize(tree.size());
        auto install = [&](int i) {
            uid[i] = inserted.size();
            inserted.push_back(i);
            queue.emplace(tree[i].time, uid[i], i);
        };
        for (size_t i = 0; i < tree.size(); ++i)
            if (tree[i].parent < 0)
                install(i);
        while (!queue.empty())
        {
            auto [time, order, id] = queue.top();
            queue.pop();
            for (size_t i = 0; i < tree.size(); ++i)
                if (tree[i].parent == id)
                    install(i);
        }
    }
};

struct Result
{
    int direct, direct_flag, cached, cached_flag, again, again_flag, prod, prod_flag;
    int selected;
    unsigned long long undecided, lost;
};

__global__ void replay(const Event *events, const int *order, int n, Chain *state, int a, int b, Sim s, Result *out)
{
    for (int i = 0; i < n; ++i)
    {
        const int id = order[i];
        const Event &x = events[id];
        if (x.parent < 0)
        {
            state[id] = {};
            state[id].root = x.root;
        }
        else
            cpush(&state[id], ccode(x.time - events[x.parent].time, x.ordinal), state[x.parent]);
    }
    const auto &A = state[a];
    const auto &B = state[b];
    // Do the unaccelerated comparison first, then exercise the memo with repeated reads.
    bool simple = false, cached = false, warm = false, fast = false;
    const int r1 = ccmp_walk(s.root_rank, 0, A, 0, B, &simple);
    const int r2 = ccmp_walk(s.root_rank, 0, A, 0, B, &cached);
    const int r3 = ccmp_walk(s.root_rank, 0, A, 0, B, &warm);
    const int r4 = ccmp(s, 0, A, 0, B, &fast);
    const int selected = before(s, events[a].time, 0, A, events[b].time, 0, B);
    *out = {r1,simple,r2,cached,r3,warm,r4,fast,selected,s.ev[8],s.ev[9]};
}

template <class T>
T *alloc(size_t count, const T *data = nullptr)
{
    T *p;
    CHECK(hipMalloc((void **)&p, count * sizeof(T)));
    if (data)
        CHECK(hipMemcpy(p,data,count*sizeof(T),hipMemcpyHostToDevice));
    else
        CHECK(hipMemset(p,0,count*sizeof(T)));
    return p;
}

int main()
{
    Fixture siblings{"siblings"};
    int root = siblings.add(-1, 0, 1);
    siblings.a = siblings.add(root, 0);
    siblings.b = siblings.add(root, 0);
    Fixture zero{"zero-delay-generation"};
    root = zero.add(-1, 0, 1);
    const int parent = zero.add(root, 0);
    zero.a = zero.add(root, 0);
    zero.b = zero.add(parent, 0);
    Fixture equal{"independent-equal-prefix"};
    root = equal.add(-1, 0, 1);
    const int other = equal.add(-1, 0, 2);
    equal.a = equal.path(root, 64);
    equal.b = equal.path(other, 64);
    Fixture fork{"fork-shared-suffix"};
    root = fork.path(fork.add(-1, 0, 1), 36);
    const int left = fork.add(root, 4);
    const int right = fork.add(root, 4);
    fork.a = fork.path(left, 44);
    fork.b = fork.path(right, 44);
    Fixture lossy{"lossy-leads"};
    root = lossy.add(-1, 1, 1);
    const int later = lossy.add(-1, 3, 2);
    lossy.a = lossy.add(root, 67108879);
    lossy.b = lossy.add(later, 67108877);

    Sim s{};
    const uint32_t ranks[] = {0,1};
    s.root_rank = alloc(2,ranks);
    s.ev = alloc<unsigned long long>(40);
    Memo memo{};
    memo.we = alloc<WalkEnt>(16384);
    memo.walk_mask = 16383;
    CHECK(hipMemcpyToSymbol(HIP_SYMBOL(g_memo),&memo,sizeof(memo)));
    bool passed = true;
    for (auto *fixture : {&siblings,&zero,&equal,&fork,&lossy})
    {
        fixture->schedule();
        if (fixture->tree[fixture->a].time != fixture->tree[fixture->b].time)
            return 1;
        const int wanted = fixture->uid[fixture->a] < fixture->uid[fixture->b] ? -1 : 1;
        for (const int capacity : fixture == &fork ? std::vector<int>{2,1024} : std::vector<int>{1024})
        {
            for (const auto &bases : {std::array<uint64_t,2>{1000003,1000033},
                                      std::array<uint64_t,2>{293,919},
                                      std::array<uint64_t,2>{331,9973}})
            {
                CHECK(hipMemcpyToSymbol(HIP_SYMBOL(c_fp),bases.data(),sizeof(bases)));
                CHECK(hipMemset(memo.we,0,16384*sizeof(WalkEnt)));
                CHECK(hipMemset(s.ev,0,40*sizeof(unsigned long long)));
                TailPool tail{alloc<TailNode>(capacity), alloc<unsigned long long>(1), uint64_t(capacity-1)};
                CHECK(hipMemcpyToSymbol(HIP_SYMBOL(g_tail),&tail,sizeof(tail)));
                const auto n = fixture->tree.size();
                auto *events = alloc(n,fixture->tree.data());
                auto *order = alloc(n,fixture->inserted.data());
                auto *state = alloc<Chain>(n);
                auto *answer = alloc<Result>(1);
                replay<<<1,1>>>(events,order,n,state,fixture->a,fixture->b,s,answer);
                CHECK(hipDeviceSynchronize());
                Result r{};
                CHECK(hipMemcpy(&r,answer,sizeof(r),hipMemcpyDeviceToHost));
                const bool flag = fixture == &lossy || capacity == 2;
                bool ok = true;
                for (const auto &v : {std::pair{r.direct,r.direct_flag},std::pair{r.cached,r.cached_flag},
                                      std::pair{r.again,r.again_flag},std::pair{r.prod,r.prod_flag}})
                    ok &= flag ? (v.second || v.first == 0) : (!v.second && v.first == wanted);
                ok &= flag ? (r.undecided || r.lost) : (!r.undecided && !r.lost && r.selected == (wanted < 0));
                passed &= ok;
                std::cout << fixture->label << ' ' << capacity << ' ' << bases[0] << ',' << bases[1] << ' '
                          << fixture->tree[fixture->a].time << ' ' << fixture->uid[fixture->a] << ' '
                          << fixture->uid[fixture->b] << " direct=" << r.direct << ',' << r.direct_flag
                          << " cached=" << r.cached << ',' << r.cached_flag
                          << " warm=" << r.again << ',' << r.again_flag
                          << " production=" << r.prod << ',' << r.prod_flag
                          << " undecided=" << r.undecided << " lost=" << r.lost << " accepted=" << ok << '\n';
                for (auto *ptr : {static_cast<void *>(tail.n),static_cast<void *>(tail.cnt),static_cast<void *>(events),
                                  static_cast<void *>(order),static_cast<void *>(state),static_cast<void *>(answer)})
                    CHECK(hipFree(ptr));
            }
        }
    }
    return passed ? 0 : 1;
}
