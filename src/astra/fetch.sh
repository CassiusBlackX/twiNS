#!/bin/bash
# fetch.sh: SimAI's system layer (astra-sim, SimCCL) at the commit TwiNS is validated against, in
# out/astra (not in this repository: Apache-2.0/MIT code of its own). What the sequential build
# needs is patched as in validation/simai (simai.patch's two lines).
set -e
cd "$(dirname "$0")/../.."
C=a57d765
D=out/astra/SimAI
if [ ! -d $D/.git ]; then
    mkdir -p out/astra
    git clone --filter=blob:none --no-checkout https://github.com/aliyun/SimAI.git $D
fi
git -C $D checkout -q $C -- astra-sim-alibabacloud/astra-sim astra-sim-alibabacloud/CMakeLists.txt 2>/dev/null ||
    git -C $D checkout -q $C -- astra-sim-alibabacloud/astra-sim
# (SimCCL: a submodule of SimAI, at the commit a57d765 records)
if [ ! -d out/astra/SimCCL/.git ]; then
    git clone --filter=blob:none --no-checkout https://github.com/aliyun/SimCCL out/astra/SimCCL
fi
git -C out/astra/SimCCL checkout -q fd7cd57
mkdir -p out/astra/inc/SimCCL
ln -sfn ../../SimCCL/src/mock/${SIMAI_NCCL_VERSION:-v2.30} out/astra/inc/SimCCL/mock
S=$D/astra-sim-alibabacloud/astra-sim
sed -i 's/^  #if defined(NS3_MTP) || defined(NS3_MPI) || defined(PHY_MTP)$/  #if 1 \/\/ TwiNS patch: the sequential build needs the NCCL mock too/' $S/system/Sys.cc
sed -i 's/^  #ifdef NS3_MTP $/  #ifndef NS3_MPI \/\/ TwiNS patch: the sequential build writes these too/' $S/workload/Workload.cc
grep -c "TwiNS patch" $S/system/Sys.cc $S/workload/Workload.cc
