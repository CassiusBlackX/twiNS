#!/usr/bin/env python3
from pathlib import Path
import json,re,time
ROOT=Path('/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116');lines=['# TwiNS 本轮实验与实现结果（2026-11-16）','','本页用于 Astra 取用已验收证据；不会修改 LaTeX。正式长实验的最新状态见 [experiment_results_live_20261116.md](experiment_results_live_20261116.md)。实验矩阵、停止规则及归档约定见 [experiment_plan_20261116.md](experiment_plan_20261116.md)。','','## 已取得结果及必须注意的变化','','A1：干净 HIP/CUDA 构建及固定底数小负载校准通过；日志改为 C_obs/n_obs 实际统计，不再打印运行后概率证书。实现前提核查见本机 `reports/a1_implementation_audit.md`。`TWINS_NO_FP` 保留，但它只关闭 FP 相等捷径，仍更新/携带 FP；不能将其时间差称为全部 FP 存储成本。','','A6：发现并复现新的 admission 边界错误。四端口混合速率拓扑中，原 safe/window 完成 1/1 流（FCT 29242ns、drops=0），同构建 serial 是 0/1、drops=1；两者均无 history flag。原 safe 条件不充分，不能在论文里仅凭该条件推广到任意 headroom 参数。候选修复在隔离分支增加 admission 充分条件，修复后 safe/serial/window 均为 0/1、drops=1、rc=2，FCT 同为空。**这代表同样 stranded，不能写成全部流完成或完整状态保证。**','','参数：B=1048576、各端口 headroom=255/343125/343125/343125、R=16384、Q=2562；低速链路 1Gbps/680ns，其余 100Gbps/9150ns。头部随 SimAI 模型计算，数学 fixture 使用 1064B 示例；真实命令和逐包 trace 路径见 `local/mmu/`。原判据的 578 组算术检查通过；含新 admission 约束的 579 组检查通过（UBSan 无报告）。合成状态与可达网络例分别记录。顺序 oracle 的 s50 二进制在本机触发 AVX-512 SIGILL（libcore 初始化的 EVEX vpbroadcastq；本机不支持），不是仿真输出差异；正在本机从同源重建可移植 oracle，尚不能把它算作已通过顺序参照。','','## A4 容量补验','','全部 24 次尝试已落本机。以下比对前六个 CSV 字段（src,dst,flow_size,start,established,completed），保留实际/参照行数及完整文件 SHA-256；额外 hash/retx 字段的全列相等另行记录，不能把这张表解读为全包轨迹比较。所有尝试无容量 overflow。','','| 用例 | 64B flags | 128B flags | 256B flags | 512B flags | 首次无 flag 且 FCT 匹配 | 达到该状态累计墙钟 s |','|---|---:|---:|---:|---:|---|---:|']
p=ROOT/'local/residuals/attempts.jsonl';xs=[json.loads(x) for x in p.read_text().splitlines()] if p.exists() else [];groups={}
for x in xs:
 log=(ROOT/f'local/residuals/{x["case"]}-runs{x["runs"]}/process.log').read_text();m=re.search(r'completed \d+ wall ([0-9.]+) s',log)
 x['loop_s_derived']=float(m[1]) if m else None
 groups.setdefault(x['case'],[]).append(x)
for n,vs in groups.items():
 vs.sort(key=lambda x:x['runs']);cum=0;first=None
 for v in vs:
  cum+=v['wall_ns']/1e9
  if first is None and not v['flagged'] and v['validation']['first_six_equal'] and not v['overflow']:first=(v['frame_bytes'],cum)
 lines.append('| '+n+' | '+' | '.join(str(int(v['flagged'])) for v in vs)+' | '+(str(first[0])+'B | '+f'{first[1]:.6f}' if first else '仍有 timer flag | —')+' |')
(ROOT/'reports/residuals-derived.json').write_text(json.dumps(xs,indent=2)+'\n')
lines+=['','NewReno/CUBIC FIN 的 timer flags 固定为 27/81；g075 为 1，扩大 lead 历史仍存在。原 TCP 结构没有 sibling ordinal；目前只实测定位到 timer 类别，尚未实现并验收新的 timer/ordinal 精确表示，不将其勾选完成。k16 spray 在 64B 的 FCT 比对失败、128B FCT 相同但有 flag、256B 起无 flag；g073 在 64B FCT 不同、128/256B FCT 相同但有 flag、512B 无 flag。','','默认工具的自动回退仍只到 128B。本轮 256/512B 是显式额外构建，不能描述为已经发布的默认能力。每次命令、二进制哈希、退出状态、逐原因日志、墙钟和校验见 `local/residuals/`。','','## 正在执行与未完成','','- A3：s50 固定八负载 × SimAI/P0/P16 × 三轮（必要时五轮），独立计时；随后做九次 safe/window 消融。实时结果每 30 秒选择性回传本机。','- 吞吐：在延迟轮次后按 CPU 1/2/4/8、70GiB cap 准入及 GPU P0/P16 顺序批次执行；仍未取得正式批量结果。','- A2：可关闭的链长/比较深度/存储计数构建、干净 FP/strict 对照在构建与排程中；计数没有跑完就不生成成本结论。完整 RLE/tail 规模、固定输入微基准与增长曲线仍待验收。','- A4：输入 inventory 已建立；历史缺失的逐 pass 墙钟明确留空，没有由最终 pass 推断。全量统一 manifest、headline 最终 flags 和新回退表示仍待补齐。','- A5：只有隔离候选，没有 merge/main、release tag 或匿名发布。本轮未修改 LaTeX。','','## 原始数据与复现位置','','本机 `twiNS/out/paper-evidence-20261116/` 保存 manifest、源码快照、编译日志、24 次容量补验、MMU 原/修复网络例及 fixtures、s50/ 下已回传正式测量。运行脚本在 `twins-paper-evidence-20261116/experiments/paper_evidence/`。远端大 trace 和临时排序缓存不回传；匹配摘要保留行数、唯一 ID 检查和哈希。']
(ROOT/'reports/experiment_results_20261116.md').write_text('\n'.join(lines)+'\n')
print('report generated',len(xs),'capacity attempts')
