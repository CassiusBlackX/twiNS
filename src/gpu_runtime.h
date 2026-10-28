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

#endif

#endif // TWINS_GPU_RUNTIME_H
