// GPU runtime abstraction: TwiNS builds with either HIP (ROCm) or CUDA. The code uses the cuda*
// names; under HIP they are mapped to hip*.
#ifndef TWINS_GPU_RUNTIME_H
#define TWINS_GPU_RUNTIME_H

#if defined(TWINS_CPU)

// The device code run on the CPU, one lane a block, blocks one after another (twins-island: a
// server's NVLink part, src/rdma/island.h). Kernels are not launched here: the island calls them.
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <algorithm>
#define __host__
#define __device__
#define __global__
#define __forceinline__ inline
#define __noinline__ __attribute__((noinline))
#define __launch_bounds__(...)
#define __align__(n) alignas(n)
#define __shared__ static thread_local
#define __constant__
struct twins_dim3
{
    unsigned x = 0, y = 0, z = 0;
};
extern thread_local twins_dim3 threadIdx, blockIdx, blockDim, gridDim;
constexpr int warpSize = 1;
inline void __syncthreads() {}
inline int __syncthreads_or(int x) { return x; }
inline void __threadfence() {}
inline void __nanosleep(unsigned) {}
inline long long clock64() { return 0; }
template <typename T>
inline T __shfl_up(T x, unsigned) { return x; }
template <typename T, typename U>
inline T atomicAdd(T *p, U v) { T o = *p; *p = o + (T)v; return o; }
template <typename T, typename U>
inline T atomicSub(T *p, U v) { T o = *p; *p = o - (T)v; return o; }
template <typename T, typename U>
inline T atomicMin(T *p, U v) { T o = *p; if ((T)v < o) *p = (T)v; return o; }
template <typename T, typename U>
inline T atomicMax(T *p, U v) { T o = *p; if ((T)v > o) *p = (T)v; return o; }
template <typename T, typename U>
inline T atomicOr(T *p, U v) { T o = *p; *p = o | (T)v; return o; }
template <typename T, typename U>
inline T atomicExch(T *p, U v) { T o = *p; *p = (T)v; return o; }
template <typename T, typename U, typename V>
inline T atomicCAS(T *p, U c, V v) { T o = *p; if (o == (T)c) *p = (T)v; return o; }
// (an island is one thread: the atomics are plain)

typedef int hipError_t;
typedef int cudaError_t;
#define hipSuccess 0
#define cudaSuccess 0
inline const char *hipGetErrorString(int) { return "out of memory"; }
#define cudaGetErrorString hipGetErrorString
inline int hipGetLastError() { return 0; }
#define cudaGetLastError hipGetLastError
inline int hipDeviceSynchronize() { return 0; }
#define cudaDeviceSynchronize hipDeviceSynchronize
inline int hipMalloc(void **p, size_t n)
{
    *p = calloc(1, n);
    return *p ? 0 : 1;
}
#define cudaMalloc hipMalloc
inline int hipFree(void *p)
{
    free(p);
    return 0;
}
inline int hipMemset(void *p, int v, size_t n)
{
    memset(p, v, n);
    return 0;
}
#define cudaMemset hipMemset
enum { hipMemcpyHostToDevice, hipMemcpyDeviceToHost, hipMemcpyDeviceToDevice };
#define cudaMemcpyHostToDevice hipMemcpyHostToDevice
#define cudaMemcpyDeviceToHost hipMemcpyDeviceToHost
inline int hipMemcpy(void *d, const void *s, size_t n, int)
{
    memmove(d, s, n);
    return 0;
}
#define cudaMemcpy hipMemcpy
typedef int hipStream_t;
#define cudaStream_t hipStream_t
inline int hipMemcpyAsync(void *d, const void *s, size_t n, int k, hipStream_t = 0) { return hipMemcpy(d, s, n, k); }
inline int hipStreamSynchronize(hipStream_t) { return 0; }
#define cudaStreamSynchronize hipStreamSynchronize
inline int hipMemGetInfo(size_t *f, size_t *t)
{
    *f = *t = 64ull << 30;
    return 0;
}
#define cudaMemGetInfo hipMemGetInfo
#define HIP_SYMBOL(x) x
#define hipMemcpyToSymbol(sym, src, n) (memcpy(&(sym), (src), (n)), 0)
#define hipMemcpyFromSymbol(dst, sym, n) (memcpy((dst), &(sym), (n)), 0)

#elif defined(TWINS_USE_HIP)

#include <hip/hip_runtime.h>

#define cudaError_t hipError_t
#define cudaSuccess hipSuccess
#define cudaGetErrorString hipGetErrorString
#define cudaDeviceSynchronize hipDeviceSynchronize
#define cudaGetLastError hipGetLastError

#define cudaMalloc hipMalloc
#define cudaMemset hipMemset
#define cudaMemGetInfo hipMemGetInfo
#define cudaMemcpy hipMemcpy
#define cudaMemcpyHostToDevice hipMemcpyHostToDevice
#define cudaMemcpyDeviceToHost hipMemcpyDeviceToHost
#define cudaHostAlloc hipHostMalloc
#define cudaHostAllocMapped hipHostMallocMapped
#define cudaHostGetDevicePointer hipHostGetDevicePointer

#define cudaStream_t hipStream_t
#define cudaStreamCreate hipStreamCreate
#define cudaStreamSynchronize hipStreamSynchronize
#define cudaStreamBeginCapture hipStreamBeginCapture
#define cudaStreamEndCapture hipStreamEndCapture
#define cudaStreamCaptureModeGlobal hipStreamCaptureModeGlobal

#define cudaGraph_t hipGraph_t
#define cudaGraphExec_t hipGraphExec_t
#define cudaGraphLaunch hipGraphLaunch
inline hipError_t twinsGraphInstantiate(hipGraphExec_t *exec, hipGraph_t graph, unsigned long long flags)
{
    return hipGraphInstantiateWithFlags(exec, graph, flags);
}
#define cudaGraphInstantiate twinsGraphInstantiate

#else

#include <cuda_runtime.h>

// twins-rdma uses the hip* names: map them to CUDA's
#define hipError_t cudaError_t
#define hipSuccess cudaSuccess
#define hipGetErrorString cudaGetErrorString
#define hipGetLastError cudaGetLastError
#define hipDeviceSynchronize cudaDeviceSynchronize
#define hipMalloc cudaMalloc
#define hipMemset cudaMemset
#define hipMemcpy cudaMemcpy
#define hipMemcpyAsync cudaMemcpyAsync
#define hipMemcpyHostToDevice cudaMemcpyHostToDevice
#define hipMemcpyDeviceToHost cudaMemcpyDeviceToHost
#define hipMemcpyToSymbol cudaMemcpyToSymbol
#define hipMemcpyFromSymbol cudaMemcpyFromSymbol
#define HIP_SYMBOL(x) x
#define hipStream_t cudaStream_t
#define hipStreamCreate cudaStreamCreate
#define hipStreamSynchronize cudaStreamSynchronize
#define hipStreamWaitEvent cudaStreamWaitEvent
#define hipStreamBeginCapture cudaStreamBeginCapture
#define hipStreamEndCapture cudaStreamEndCapture
#define hipStreamCaptureModeGlobal cudaStreamCaptureModeGlobal
#define hipEvent_t cudaEvent_t
#define hipEventCreateWithFlags cudaEventCreateWithFlags
#define hipEventDisableTiming cudaEventDisableTiming
#define hipEventRecord cudaEventRecord
#define hipGraph_t cudaGraph_t
#define hipGraphExec_t cudaGraphExec_t
#define hipGraphLaunch cudaGraphLaunch
inline cudaError_t hipGraphInstantiate(cudaGraphExec_t *exec, cudaGraph_t graph, void *, void *, unsigned long long flags)
{
    return cudaGraphInstantiate(exec, graph, flags);
}
#define hipFuncSetAttribute cudaFuncSetAttribute
#define hipFuncAttributeMaxDynamicSharedMemorySize cudaFuncAttributeMaxDynamicSharedMemorySize
#define hipOccupancyMaxActiveBlocksPerMultiprocessor cudaOccupancyMaxActiveBlocksPerMultiprocessor
#define hipGetDevice cudaGetDevice
#define hipGetDeviceProperties cudaGetDeviceProperties
#define hipDeviceProp_t cudaDeviceProp
#define hipDeviceGetAttribute cudaDeviceGetAttribute
#define hipDeviceAttributeClockRate cudaDevAttrClockRate
// (no wall-clock counter attribute in CUDA: clock64() counts SM cycles)
#define hipDeviceAttributeWallClockRate cudaDevAttrClockRate
// (GCC's bit builtins do not reach CUDA's device code; they are used there alone)
#define __builtin_ctz(x) (__ffs(x) - 1)
#define __shfl_up(x, o) __shfl_up_sync(0xffffffffu, x, o)

#endif

#endif // TWINS_GPU_RUNTIME_H
