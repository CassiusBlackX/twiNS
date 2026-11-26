# 11/27 统一实验

本目录对应 `materials/TODOS.md` 的 11/27 清单。只更新实现、验证工具与实验记录，不修改论文 TeX。完整本机回执位于 `out/unified-review/REPORT.md`；原始失败、负对照和被替代的试跑单独保留。

普通入口新增或补全的能力：

- RDMA 发送导出按 `(time,node,port,capture-index)` 排列，IRN 扩展字段仍按原采集索引读取。`--txtrace-cap=N` 明确区分完整记录和截断，截断返回非零。
- RDMA 配置及批处理保留每世界的排他纳秒截止；`--stop-ns` 可显式覆盖。跨半槽截止使用保守执行，同一对齐截止保持窗口推进。容量恢复只重跑受影响世界，保留参考也未完成的世界状态。
- TCP 的常规驱动支持 `--arrival-layout=auto|split|wide`。短前缀默认从 4,096 个尾部位置开始，按观察到的高水位扩容；`--arrival-tail-cap=N` 表示固定容量，不能容纳时明确失败。`--start-history=2048` 选择 127-run 深度。ROCm Clang 22 的紧凑目标使用实测通过的 `-O0` 设置；其他构建沿用已有编译设置。
- TCP 分开报告活跃表、分片计时器及分片记录存储不足。驱动扩容相应存储，避免为活跃表扩容时一并按每地址对分配 65,536 个分片记录。
- RDMA `--q3-stop-grace-ns=0|100000|-1` 分别可测立即早停、短收集及整遍收集。默认先收集 100 µs；发现多热点后保留整遍收集，避免逐端口反复重试。`--q3-auto --reuse-topology` 保留拓扑复用；失败尝试计入完整调用墙钟。
- 冻结协议生成物通过 `-DTWINS_POLICY_DIR=...` 接入普通构建，支持 `--tcp=scalable|highspeed` 及与原三协议混合批。不会修改生成器或生成的状态转移。
- `TWINS_CLOSED_CALLBACK_TRACE` 导出真实回调入口顺序；`TWINS_CLOSED_HORIZON_TRACE` 额外记录视界及每次回调实际执行时的排他上界。`validation/simai/compare_closed.py` 检查完整消息身份、多重数、父动作、setup 相对序和可选的执行日志。

复验入口与输出：

| 清单 | 脚本／回执 |
| --- | --- |
| 1 | 已冻结的 `twins-paper-review/experiments/paper_strict/pfc_send_order.py`；`pfc-{hip,cuda}/summary.json`、`pfc-checker-controls.json` |
| 2 | `rdma_cutoff.py`、`verify_cutoff.py`、`rdma_mixed.py`、`aligned_batch.py` |
| 3 | `tcp_storage.py`；`storage-hip-release/`、`storage-cuda/`，及 `tcp-mixed-o0-hip/acceptance.json` |
| 4 | `queue_recovery.py`、`final_checks.py`；`queue-cuda/`、`queue-hip-adaptive/` |
| 5 | `lineage_order.cu`；`lineage-flags-{hip,cuda}.log` |
| 6 | `mmu_recheck.sh`、`mmu_verify.py`、`mmu_admission.sh`、`mmu_pause_edge.sh`、`irn_stream.sh`、`final_checks.py` |
| 7 | `native_callbacks.py`、`real_join.sh`、`verify_real_join.py`、`checker_controls.py`；`real-join-{hip,cuda}/acceptance.json` |
| 8 | `tcp_close.sh`、`tcp_route_close.sh`、`fin_directed.sh`；`tcp-close-acceptance.json` |
| 9 | `figure_tcp.py`、`figure_receipt.py`、`phase_observer.py`；`figure-acceptance.json` |
| 10 | `freeze_holdout.py`、`holdout_run.py`、`verify_holdout.py`、`case_replays.py`；额外有损样本用 `freeze_loss.py` 和 `loss_holdout.py`，不并入原 12+2 分母 |
| 11 | `policy_entry.py`；`policy-{hip,cuda}/summary.json` 及两份 HIP hook 输出 |

GPU 运行使用本机 GPU 锁或 s50 原队列的 `exclusive-campaign.lock`。纯 CPU 顺序参照及 HIP 编译放在 a87；图 6 同机 CPU 性能与初始化诊断按任务要求使用 s50 的 9950X，且等待原队列结束。每个正式脚本保存命令、退出状态和原始日志；用具体输入、共同截止及独立参照验收，不以完成条数或两后端互相一致代替正确性。

性能的三轮完整调用与只推进一槽的初始化观测分开。后者报告初始容量的一次路由、分配和图建立，不能当作所有重试的初始化总和。CUBIC 的 100 ms 截止包含参考本身的未完成流，成功标准是同一身份及时间，而非人为延长到全部完成。Unison 样本固定 32 线程，没有将其称为现代选项设置下的最快线程数。
