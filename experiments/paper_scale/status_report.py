"""Write a local paper handoff from the archived scientific receipts."""
from pathlib import Path
import json,sys
r=Path(sys.argv[1])
summary=json.loads((r/'summary.json').read_text())
p=r/'REPORT.md'
with p.with_suffix('.pending').open('w') as f:
    def line(text=''):f.write(text+'\n')
    line('# TwiNS 新增论文实验');line()
    line('2026-11-19。本机 RX 9060 XT 与 a87 的顺序参照已完成。9950X 单核及 RTX 5090 成对计时由本机持久队列在旧实验结束后启动，每点预热一轮，正式三轮。本文只汇报已经核对的输出。');line()
    line('## 异构 TCP');line()
    line('1,024 个主机各发送十条 32 KiB 流。25 Gb/s、500 ns 接入与 100 Gb/s、2 µs 上联组合，以及统一 100 Gb/s、1 µs 对照的 10,240 条流，分别与顺序 ns-3.46.1 的六个字段全部一致。两种拓扑在 128、256、512 主机时也全部对齐。预留种子 102 的两种起始帧宽与其独立顺序输出均已核对。');line()
    s=summary['tcp']
    for name in ['heterogeneous-start-64','heterogeneous-start-512','holdout-start-64','holdout-start-512']:
        v=s[name];line(f"- `{name}` 完整进程 {v['wall_ns']/1e9:.3f} 秒，单卡采样峰值 {v['peak_sampled_bytes']/2**30:.2f} GiB。")
    line();line('以上为 RX 独立诊断的单次观察。异构路径实际帧宽为 96、160、288、544 字节。500 ns 槽、7 个环桶和 544 字节帧在万流图上为到达环分配 3,509,796,864 字节。gfx1200 的 ROCm clang 22 在 31 游程优化内核上可重现内存访问故障。该目标以 -O0 重编通过，GPU 启动策略仍从窄帧开始。');line()
    line('## 有损 IRN 和 DCQCN');line()
    line('冻结原生作者分支 236a801、PFC 关闭、1 MiB 缓冲、150,000 字节/流。正式 DCQCN 组打开 ECN，标记区间 32–64 kB，最大标记概率 0.2。八个设置都完成了全部流。');line()
    line('| 主机 | 分散逐流匹配 | incast 逐流匹配 | incast GPU 丢包 / 重传包 / 超时 |');line('|---:|---:|---:|---:|')
    for h in (128,256,512,1024):
        a=summary['dcqcn_irn'][f'spread-{h}'];b=summary['dcqcn_irn'][f'incast-{h}']
        line(f"| {h} | {a['matched']:,}/{a['input_flows']:,} | {b['matched']:,}/{b['input_flows']:,} | {b['gpu_drops']:,} / {b['gpu_retransmitted']:,} / {b['gpu_timeouts']:,} |")
    line();line('原始锚点配置关闭交换机标记，分散万流一致，incast 在 128 主机通过，256 主机起有分歧。带标记的 incast 在 128 主机已出现分歧。每条抽样物理链路的首处分叉和设备时钟区间均存入相应的 comparison.json。');line()
    for case in ('irn-incast-256','irn-spread-1024','irn-incast-1024'):
        q=json.loads((r/'dcqcn/reference-traces'/case/'comparison.json').read_text());v=q['profile']
        equal=sum(x['first_difference'] is None for x in q['links'].values());t=v['ticks']
        line(f"- {case} 的四条链路中 {equal} 条全部发送记录相同。{v['sample_slots']} 个执行过的采样槽内，串行段并集占设备有工作时间的 {v['serial_fraction']:.1%}，其中 {t['also_endpoint']/t['serial_union']:.1%} 的串行段时间与端点重叠。活跃交换机槽中，多端口活跃的比例为 {v['multiple_fraction']:.1%}。")
    line();line('incast 的顺序参照验收仍有分歧。交换机并行优化保留为待验项。');line()
    line('## ECN 平局扰动');line()
    line('a87 完成 480 组。RX 独立复跑的 80 组原插入序结果与顺序参照逐流一致。下表每行含相同的 20 条到达轨迹。Δ 为 K40 减 K30 的短流 p99，排序比较 K10、K20、K30。');line()
    line('| 平局顺序 | 从低于 1 ms 跨至至少 1 ms | Δ 中位数（µs） | 排序保持 / 改变 / 并列 |');line('|---:|---:|---:|---:|')
    for e in summary['ecn']:
        rank=e['ranking'];line(f"| {e['order']} | {e['crossings']}/20 | {e['median_ns']/1000:.3f} | {rank.get('preserved',0)} / {rank.get('changed',0)} / {rank.get('tied',0)} |")
    line();line('预选轨迹 1、K30 中，85,716,840 ps 时同一包在原序看到队长 31 并被标记，扰动序看到队长 30。该轨迹的短流 p99 从 149.469 µs 变为 155.000 µs。');line()
    line('## 有损 ASTRA 闭环');line()
    v=summary['ai']['small-buffer'];w=summary['ai']['large-buffer']
    line(f"相同拓扑和两阶段 AllToAll 各产生 480 条消息。64 MiB 缓冲与顺序 IRN + ASTRA 的 {w['matching_messages']} 条消息、{w['matching_actions']:,} 条规范化动作及恢复计数全部匹配。1 MiB 缓冲完成 480 条，{v['matching_messages']} 条消息与 {v['matching_actions']:,}/{v['gpu_actions']:,} 条动作匹配。")
    line();line('小缓冲下，消息 120（8→0）出现选择性反馈并重传，完成回调通过定时器 t209 释放消息 240（0→1）。顺序参照中后继在 6,380,999 ns 发起，RX 在 6,380,995 ns 发起。大缓冲下，同一后继在两端均于 3,551,913 ns 发起，释放它的前驱也发生了变化。两端发送记录、回调链及各自首处分叉均已归档。128 卡扩展仍待小型有损对照通过。')
    line();line('## 本机交付');line()
    line('新实验源码在 twins-paper-scale 工作树。原始证据、命令、输入及二进制清单位于 out/campaign。RX/a87 阶段归档名为 twins-paper-scale-rx-a87-20261119.tar.zst，归档在 a87 解包逐文件校验后返回本机。后续 5090 每组先带校验回传本机，再释放 s50 的临时原始输出。')

p.with_suffix('.pending').replace(p)
