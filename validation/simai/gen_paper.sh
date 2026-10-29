#!/bin/bash
# gen_paper.sh: the SimAI paper's (NSDI'25, §4) workloads, in ~/netsim/aiwork/wl/paper on a87.
# - collectives across hosts (§4.2): one collective per file, in the data-parallel group with TP 8
#   (a GPU's group is the same GPU of every server: one rail), at 128, 512 and 1024 GPUs;
# - training (§4.4, Table 3): GPT-3 13B (TP 2), LLaMA 65B (TP 8, PP 2), GPT-3 175B (TP 8, PP 8),
#   from AICB, with full layers, mid (8 or 16 layers) and 2 layers per pipeline stage (probes). A pipeline runs PP
#   micro-batches (ga = PP), micro-batch 1.
cd ~/netsim/simai/aicb
W=~/netsim/aiwork/wl/paper
mkdir -p $W
coll() { # NAME TYPE BYTES GPUS
    printf 'HYBRID_TRANSFORMER_FWD_IN_BCKWD model_parallel_NPU_group: 8 ep: 1 pp: 1 vpp: 8 ga: 1 all_gpus: %d checkpoints: 0 checkpoint_initiates: 0\n1\n' $4 > $W/$1.txt
    printf 'coll_layer\t-1\t1\tNONE\t0\t1\tNONE\t0\t1\t%s\t%d\t1\n' $2 $3 >> $W/$1.txt
}
for g in 128 512 1024; do
    for t in ALLREDUCE ALLGATHER REDUCESCATTER; do
        for m in 8 64 512; do
            coll c_$(echo $t | tr A-Z a-z)_${m}m_$g $t $((m * 1048576)) $g
        done
    done
done
train() { # NAME LAYERS GPUS TP PP ARGS...
    n=$1 l=$2 g=$3 tp=$4 pp=$5; shift 5
    dp=$((g / tp / pp))
    f=$(~/netsim/venv-aicb/bin/python -m workload_generator.SimAI_training_workload_generator --world_size=$g \
        --tensor_model_parallel_size=$tp --pipeline_model_parallel=$pp --global_batch=$((dp * pp)) --micro_batch=1 \
        --num_layers=$l "$@" 2>&1 | grep "workload save" | sed "s/.*: //")
    [ -f "$f" ] && cp "$f" $W/$n.txt && echo "$n: $(sed -n 2p $W/$n.txt) layers, $(wc -l < $W/$n.txt) lines" || echo "$n: FAILED"
}
for g in 128 512 1024; do
    # full: the paper's layers; mid: 8 (GPT-3 13B, LLaMA 65B) and 16 (GPT-3 175B, 2 per stage), where
    # a full iteration takes SimAI over a day (its gradient collectives grow with the layers);
    # probe: 2 layers per stage
    for v in full mid probe; do
        case $v in
        full) gl=40 ll=80 cl=96 ;;
        mid) gl=8 ll=8 cl=16 ;;
        probe) gl=2 ll=4 cl=16 ;;
        esac
        train gpt3_13b_${v}_$g $gl $g 2 1 --num_attention_heads=40 --hidden_size=5120 --ffn_hidden_size=20480 --seq_length=2048
        train llama_65b_${v}_$g $ll $g 8 2 --num_attention_heads=64 --hidden_size=8192 --ffn_hidden_size=28672 --seq_length=4096 --swiglu
        train gpt3_175b_${v}_$g $cl $g 8 8 --num_attention_heads=96 --hidden_size=12288 --ffn_hidden_size=49152 --seq_length=2048
    done
done
ls $W | wc -l
