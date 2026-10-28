#!/bin/bash
# AICB SimAI training workloads (no torch: computation_enable off, AICB default compute times)
cd ~/netsim/simai/aicb
gen() {
    out=$1; shift
    f=$(~/netsim/venv-aicb/bin/python -m workload_generator.SimAI_training_workload_generator "$@" 2>&1 | grep "workload save" | sed "s/.*: //")
    cp "$f" ~/netsim/aiwork/wl/$out && echo "$out: $(head -1 ~/netsim/aiwork/wl/$out | cut -c1-110) | $(sed -n 2p ~/netsim/aiwork/wl/$out) layers"
}
W=$1
gen llama_$W.txt --world_size=$W --global_batch=$W --micro_batch=1 --num_layers=8 --num_attention_heads=32 --hidden_size=4096 --tensor_model_parallel_size=8 --seq_length=2048 --swiglu --ffn_hidden_size=11008
gen gpt13_$W.txt --world_size=$W --global_batch=$W --micro_batch=1 --num_layers=8 --num_attention_heads=40 --hidden_size=5120 --tensor_model_parallel_size=8 --seq_length=2048 --ffn_hidden_size=20480 --enable_sequence_parallel
gen moe_$W.txt --world_size=$W --global_batch=$W --micro_batch=1 --num_layers=4 --num_attention_heads=32 --hidden_size=4096 --tensor_model_parallel_size=2 --seq_length=2048 --swiglu --ffn_hidden_size=11008 --moe_enable --expert_model_parallel_size=8 --num_experts=8 --moe_router_topk=2 --enable_sequence_parallel
