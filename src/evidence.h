// Default-off observational instrumentation. Distributions are push/comparison weighted,
// not distributions of distinct live lineages. Counters can perturb GPU scheduling and cost.
#ifndef TWINS_EVIDENCE_H
#define TWINS_EVIDENCE_H
#ifdef TWINS_EVIDENCE
namespace twins_evidence {
__device__ unsigned long long count[16], lengths[32], runs_hist[32], depths[32];
__device__ uint32_t *tail_depth=nullptr;
inline unsigned long long instrumentation_bytes=0;
inline unsigned long long allocated=0,inline_allocated=0;
__host__ __device__ inline int bucket(unsigned long long n) {
 int b=0;while(n>1 && b<31){n>>=1;b++;}return b;
}
__host__ __device__ inline unsigned long long push(unsigned long long n,unsigned long long runs,bool extended,bool tail,bool truncated=false) {
 unsigned long long index=0;
#if defined(__HIP_DEVICE_COMPILE__) || defined(__CUDA_ARCH__) || defined(TWINS_CPU)
 index=atomicAdd(&count[0],1ull);atomicAdd(&count[1],n);atomicAdd(&count[2],runs);
 atomicAdd(&count[extended?3:4],1ull);if(tail)atomicAdd(&count[5],1ull);
 if(truncated)atomicAdd(&count[6],1ull);
 atomicAdd(&lengths[bucket(n)],1ull);atomicAdd(&runs_hist[bucket(runs)],1ull);
 atomicMax(&count[7],n);
#endif
 return index;
}
__host__ __device__ inline void comparison(unsigned long long steps) {
#if defined(__HIP_DEVICE_COMPILE__) || defined(__CUDA_ARCH__) || defined(TWINS_CPU)
 atomicAdd(&depths[bucket(steps)],1ull);atomicAdd(&count[8],steps);atomicAdd(&count[9],1ull);
#endif
}
inline void report(unsigned long long tail_allocated=0,unsigned long long tail_slots=0,unsigned long long tail_bytes=0,unsigned long long memo_bytes=0) {
 unsigned long long c[16],l[32],r[32],d[32];
 hipMemcpyFromSymbol(c,HIP_SYMBOL(count),sizeof(c));hipMemcpyFromSymbol(l,HIP_SYMBOL(lengths),sizeof(l));
 hipMemcpyFromSymbol(r,HIP_SYMBOL(runs_hist),sizeof(r));hipMemcpyFromSymbol(d,HIP_SYMBOL(depths),sizeof(d));
 printf("evidence_schema: v2 full_rle=auxiliary_depth sample_period=4096 instrumentation_bytes=%llu\n",instrumentation_bytes);
 printf("evidence_counts:");for(auto x:c)printf(" %llu",x);printf("\n");
 printf("evidence_length_log2:");for(auto x:l)printf(" %llu",x);printf("\n");
 printf("evidence_inline_runs_log2:");for(auto x:r)printf(" %llu",x);printf("\n");
 printf("evidence_comparison_steps_log2:");for(auto x:d)printf(" %llu",x);printf("\n");
 printf("evidence_storage: tracked_device_allocated=%llu inline_allocated=%llu tail_allocations=%llu tail_slots=%llu tail_bytes=%llu memo_bytes=%llu tail_overwrite_count=%llu\n",allocated,inline_allocated,tail_allocated,tail_slots,tail_bytes,memo_bytes,tail_allocated>tail_slots?tail_allocated-tail_slots:0);
}
}
#endif
#endif
