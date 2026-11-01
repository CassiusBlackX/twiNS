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
# Copies of whole flows, with their vectors, per flow and per callback: a quarter of the system layer's
# time in malloc and free, with no effect on what it does (references to the same flows, only read).
F=$S/system/collective/NcclFlowModel.cc
sed -i 's/for(const auto flow_model : _flow_models) {/for(const auto \&flow_model : _flow_models) { \/\/ TwiNS patch: no copy/' $F
sed -i 's/^\( *\)std::vector<int> parent_list = flow_model.second.parent_flow_id;/\1const std::vector<int> \&parent_list = flow_model.second.parent_flow_id; \/\/ TwiNS patch: no copy/' $F
sed -i 's/^\( *\)for(auto flow : _flow_models){/\1for(const auto \&flow : _flow_models){ \/\/ TwiNS patch: no copy/' $F
sed -i 's/^\( *\)MockNccl::SingleFlow cur_flow = _flow_models\[/\1const MockNccl::SingleFlow \&cur_flow = _flow_models[/' $F
sed -i 's/^\( *\)MockNccl::SingleFlow old_flow =$/\1const MockNccl::SingleFlow \&old_flow =/' $F
sed -i 's/^\( *\)MockNccl::SingleFlow f = _flow_models\[/\1const MockNccl::SingleFlow \&f = _flow_models[/' $F
sed -i 's/^\( *\)auto flow_model = this->_flow_models\[/\1const auto \&flow_model = this->_flow_models[/' $F
sed -i 's/^\( *\)std::vector<int> current_sender = f.prev;/\1const std::vector<int> \&current_sender = f.prev;/' $F
sed -i 's/^\( *\)for(auto f : \*ptr_flow_models) {/\1for(const auto \&f : *ptr_flow_models) { \/\/ TwiNS patch: no copy/' $F
sed -i 's/^\( *\)MockNccl::SingleFlow flow_model =$/\1const MockNccl::SingleFlow \&flow_model =/' $F
