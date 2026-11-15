#!/usr/bin/env python3
"""Auto-generated Markdown from local receipts; never edits LaTeX or manual results prose."""
from pathlib import Path
import datetime,json,re,statistics,time,subprocess,os
ROOT=Path('/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116');MAT=Path('/home/cassius/Projects/TwiNS')
def read(p):
 try:return json.loads(p.read_text())
 except (OSError,ValueError):return None
while True:
 # Keep orthogonal status derivation separate from immutable raw receipts.
 with (ROOT/'receipts/derive-unified.log').open('a') as f:subprocess.run(['/usr/bin/python3',str(Path(__file__).with_name('derive_unified.py'))],stdout=f,stderr=subprocess.STDOUT,timeout=45)
 # Derived tables/figures refresh only when receipts change; scientific outputs remain untouched.
 plotpython=Path('/tmp/twins-paper-plot-env/bin/python')
 if plotpython.exists():
  with (ROOT/'receipts/derive-a2.log').open('a') as f:subprocess.run([str(plotpython),str(Path(__file__).with_name('derive_a2.py'))],stdout=f,stderr=subprocess.STDOUT,timeout=45,env={**os.environ,'MPLCONFIGDIR':'/tmp/twins-mpl'})
 table=ROOT/'reports/a2-paper-tables.md'
 if table.exists():(MAT/'a2_cost_results_20261116.md').write_text(table.read_text())
 now=datetime.datetime.now().astimezone().isoformat();lines=['# TwiNS 本机补验实时汇总','',f'自动更新时间：{now}。本页只汇总已存在的结果文件；后台队列已启动不代表验收完成。','','## A6 边界验证与修复','','原四端口反例：safe/window 为 1/1 流、drops=0，serial 和顺序 oracle 为 0/1、drops=1，原程序都无 history flag。隔离候选 `48e0a3a` 补充 ingress-admission 条件；匹配 stranded 状态不称为全部完成。', '']
 rows=read(ROOT/'local/mmu/boundaries_v3/results.json') or []
 # Individual receipts remain authoritative if an intermediate aggregate predates its files.
 individual=[read(p) for p in (ROOT/'local/mmu/boundaries_v3').glob('*/*/result.json')];individual=[x for x in individual if x];rows=individual or rows
 lines+=['| 场景 | 模式 | FCT 行数 | 公共边界×端口 | 状态差异 | safe/观测 | 多槽观测 | 输出/边界通过 |','|---|---|---:|---:|---:|---:|---:|---|']
 for x in sorted(rows,key=lambda x:(x['case'],x['variant'])):lines.append(f'| {x["case"]} | {x["variant"]} | {x["actual_rows"]} | {x["common_boundary_ports"]} | {x["boundary_mismatches"]} | {x["safe_boundaries"]}/{x["observed_boundaries"]} | {x["multislot_boundaries"]} | {x["reference_agreement"]} |')
 lines+=['','PFC 用例实际发生 18 次 pause 与 18 次 resume；NVLink idle 小拓扑实际覆盖 global-window 多槽执行及 forced-serial 回退。原先只有名字却未触发 PFC/多槽的 v1/v2 记录保留，不用于声称覆盖。边界比较针对 `<t` 的状态；参照结束后的 GPU 边界未比较。24/24 个测试的发送字段多重集及逐链路顺序也一致（`tx-validation.json`）；未声称比较了未记录的完整协议状态。完整逐次命令、标志、整数状态与版本在 `local/mmu/`。','','## A4 独立 timer 表示','','`TWINS_TCP_TIMER_ORDER` 默认关闭；本轮试验在 128B 保存定时器在主机内的创建顺序，不通过扩大帧内 lead 容量掩盖未知信息。每连接增加六个 64 位键、每主机一个 64 位计数器。','','| 场景 | rc | FCT 匹配 | flag | wall 秒 |','|---|---:|---|---|---:|']
 for p in sorted((ROOT/'local/residuals-timer').glob('*/result.json')):
  x=read(p)
  if x:lines.append(f'| {x["case"]} | {x["rc"]} | {x["validation"].get("first_six_equal")} | {x["flagged"]} | {x["wall_ns"]/1e9:.6f} |')
 lines+=['','三个定位案例已通过；完整主集回归与是否纳入默认实现仍分别记录，不能改写历史默认流程。','','## A2 同版本受控测量','','最终对照使用冻结候选源码，仅构建定义不同。早期 `local/profile/` 数据单列为预备测量；不能与最终版本混合求中位数。短容量 TCP 的 flags 保留；长历史机制解释以通过验证的 256B 设置为准。','','| 场景 | 构建 | 已完成/通过 | 中位数秒 | 范围秒 |','|---|---|---:|---:|---:|']
 groups={}
 for p in (ROOT/'local/profile-final').glob('*/result.json'):
  x=read(p)
  if x:groups.setdefault((x['case'],x['variant']),[]).append(x)
 for (n,v),xs in sorted(groups.items()):
  ts=[x['wall_ns']/1e9 for x in xs]
  lines.append(f'| {n} | {v} | {len(xs)}/{sum(x["accepted"] for x in xs)} | {statistics.median(ts):.9f} | {min(ts):.9f}–{max(ts):.9f} |')
 lines+=['','计数、log2 分布、RLE 抽样、tail 分配/复用/覆盖、明确分配字节及带桌面背景的 VRAM 采样在逐次结果和日志中。TCP 截断时是记录前缀长度；RDMA 每 4096 push 的完整 RLE 抽样把失效 tail 单列。计数构建开销单独报告，不与干净版本混用。','','## A2 固定输入 GPU 微基准','','三组语料来自真实比较调用，采样位置是比较次数的 2 的幂。FP 覆盖 root-start 之后的后缀，walk 输入还含 root-start 前缀；旧 v1 范围错误记录保留且不采用。后续已通过检查的样本才可使用。微基准重建连续 tail 布局，不能把其缓存局部性等同生产模拟，也不能把阶段时间相加当墙钟。','','| 语料 | 验收 | 阶段 | 中位 GPU 毫秒 |','|---|---|---:|']
 for folder in sorted((ROOT/'local/corpus').glob('*')):
  corrected=(folder/'micro-isolated-result.json').exists()
  p=folder/('micro-isolated-result.json' if corrected else 'micro-final-result.json' if (folder/'micro-final-result.json').exists() else 'micro-v2-result.json')
  x=read(p)
  if not x:continue
  phases={}
  for r in x.get('records',[]):
   if 'phase' in r and 'gpu_ms' in r and (corrected or r['phase']!='fingerprint_shortcut'):phases.setdefault(r['phase'],[]).append(r['gpu_ms'])
  for phase,ts in phases.items():lines.append(f'| {p.parent.name} | {x["accepted"]} | {phase} | {statistics.median(ts):.9f} |')
 lines+=['','每轮 untimed warmup 后计三次，保留全部旧版本。micro-final 的 fingerprint_shortcut 含语料捕获钩子，时间排除；待 micro-isolated-result.json（直接调用 production ccmp_impl，core6bc5a7c）完成后才显示该阶段。其他阶段无此钩子。所有阶段记录二进制哈希；warm memo 裁决逐项检查。','','## 增长曲线与统一 manifest','','四个缩放输入开始时刻不变，仅 flow_size ×1/2/4/8；新 HPCC 参照全部回传本机。严格模式每例一小时上限，超时是删失数据。','','| 倍率 | 模式 | rc | FCT 相同 | flag | wall 秒 |','|---|---|---:|---|---|---:|']
 for p in sorted((ROOT/'local/growth').glob('*/result.json')):
  x=read(p)
  if x:lines.append(f'| {x["scale"]} | {x["variant"]} | {x["rc"]} | {x["equal"]} | {x["flagged"]} | {x["wall_ns"]/1e9:.6f} |')
 status={}
 for p in (ROOT/'local/unified').glob('*/result.json'):
  x=read(p)
  if x:status[x['final_status']]=status.get(x['final_status'],0)+1
 timer_status={}
 for p in (ROOT/'local/timer-regression').glob('*/result.json'):
  x=read(p)
  if x:timer_status[x['final_status']]=timer_status.get(x['final_status'],0)+1
 lines+=['',f'统一新执行状态：`{json.dumps(status,ensure_ascii=False)}`。清单包含显式集合交叠；历史缺失 pass 的墙钟保留未知，不从末次日志推断。',f'timer 主集/网格回归：`{json.dumps(timer_status,ensure_ascii=False)}`。局部三例成功不代表这批已经全部完成。','','重要原始记录均在本机 `twiNS/out/paper-evidence-20261116/`；普通长运行 trace 不回传。LaTeX 由 Astra 维护，本实验会话只发布此类 Markdown 结果入口。']
 text='\n'.join(lines)+'\n'
 summary=ROOT/'reports/unified-summary.md'
 if summary.exists():text+='\n'+summary.read_text()
 (ROOT/'reports/experiment_results_local_live_20261116.md').write_text(text);(MAT/'experiment_results_local_live_20261116.md').write_text(text)
 if (ROOT/'local/all-finished.json').exists() and (ROOT/'receipts/hip-boundary-result.json').exists():break
 time.sleep(30)
