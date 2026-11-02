// GPU runtime abstraction: TwiNS builds with either HIP (ROCm) or CUDA. The code uses the cuda*
// names; under HIP they are mapped to hip*.
#ifndef TWINS_GPU_RUNTIME_H
#define TWINS_GPU_RUNTIME_H

#if defined(TWINS_USE_HIP)

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

#endif

#endif // TWINS_GPU_RUNTIME_H
