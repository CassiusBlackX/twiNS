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
# (two scans of free_packets, a callback each, were over half of the system layer's time with many
# channels: one's result is not used; the other asks whether any entry is non-zero, now a count)
python3 - "$F" "${F%.cc}.hh" <<'PY'
import sys
cc, hh = sys.argv[1:3]
t = open(cc).read()
def once(a, b):
    global t
    assert t.count(a) == 1, a
    t = t.replace(a, b)
once("""    bool recv_finished_tag = true;
    for (auto it = free_packets.begin(); it != free_packets.end(); it++) {
      if (it->second != 0) {
        recv_finished_tag = false;
        break;
      }
    }
""", "")
once("""  for (auto it = free_packets.begin(); it != free_packets.end(); it++) {
    if (it->second != 0) {
      all_packets_freed = false;
      break;
    }
  }
""", """  all_packets_freed = free_nonzero == 0; // TwiNS patch: a count, not a scan
""")
once("""this->free_packets[std::make_pair(f.second.channel_id,f.second.src)]++;""",
     """{ int &v = this->free_packets[std::make_pair(f.second.channel_id,f.second.src)]; free_nonzero += (v + 1 != 0) - (v != 0); v++; } // TwiNS patch""")
once("""free_packets[std::make_pair(channel_id, flowTag.sender_node)]--;""",
     """{ int &v = free_packets[std::make_pair(channel_id, flowTag.sender_node)]; free_nonzero += (v - 1 != 0) - (v != 0); v--; } // TwiNS patch""")
# (a stream's start walked all of the rank's flows once per channel, over half of the stub's time on a
# many-channel training run: the same flows, in the same order, by channel)
once("""      for(int j = 0; j < m_channels; j ++) {
        for(const auto &flow_model : _flow_models) { // TwiNS patch: no copy
""", """      std::vector<std::vector<const MockNccl::FlowModels::value_type *>> by_channel(m_channels); // TwiNS patch
      for(const auto &fm : _flow_models)
        if(fm.second.src == id && fm.second.parent_flow_id.size() == 0 && fm.second.channel_id >= 0 && fm.second.channel_id < m_channels)
          by_channel[fm.second.channel_id].push_back(&fm);
      for(int j = 0; j < m_channels; j ++) {
        for(const auto *fmp : by_channel[j]) {
          const auto &flow_model = *fmp;
""")
# (iteratable asks every channel's count in a map, per callback)
once("""        this->_stream_count[f.second.channel_id] += 1;
""", """        { int &v = this->_stream_count[f.second.channel_id]; if (v == 0 && f.second.channel_id >= 0 && f.second.channel_id < m_channels) stream_nonzero++; v += 1; } // TwiNS patch
""")
once("""    _stream_count[channel_id]--;
""", """    { int &v = _stream_count[channel_id]; v--; if (v == 0 && channel_id >= 0 && channel_id < m_channels) stream_nonzero--; } // TwiNS patch
""")
once("""    bool tag = true;
    for (int i = 0; i < m_channels; i++) {
      if (_stream_count[i] != 0) {
        tag = false;
        break;
      }
    }
""", """    bool tag = stream_nonzero == 0; // TwiNS patch
""")
once("""  for(int i = 0; i < m_channels; ++ i) {
    if(_stream_count.count(i) != 0 && _stream_count[i] != 0) all_channel_finished = false;
  }
""", """  all_channel_finished = stream_nonzero == 0; // TwiNS patch
""")
open(cc, "w").write(t)
t = open(hh).read()
once("""  std::map<int, int> _stream_count; """, """  std::map<int, int> _stream_count; 
  int stream_nonzero = 0; // TwiNS patch: channels below m_channels whose count is not zero""")
once("""  std::map<std::pair<int,int>, int> free_packets; """, """  std::map<std::pair<int,int>, int> free_packets; 
  int free_nonzero = 0; // TwiNS patch: its entries that are not zero""")
open(hh, "w").write(t)
PY
