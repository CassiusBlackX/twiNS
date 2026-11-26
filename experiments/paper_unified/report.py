"""Assemble a concise local index without rewriting the paper's TeX or TODOs."""
from pathlib import Path
import json,re
from statistics import median
from datetime import datetime
r=Path(__file__).resolve().parents[2]/'out/unified-review'
def load(p):return json.loads((r/p).read_text())
fig=load('figure-acceptance.json')
assert len(fig)==6
for key,v in fig.items():
 assert all(a['turns']==3 for a in v.values())
 assert all(x['six_field_differences']==0 for x in v['twins']['observations'])
 assert {x['retx']for x in v['twins']['observations']}=={x['retx']for x in v['ns3']['observations']}
assert len(load('figure-setup-positive/summary.json'))==18
assert all(x['rc']==0 for x in load('figure-setup-positive/summary.json'))
for arch in ['hip','cuda']:
 assert len(load(f'cutoff-{arch if arch=="cuda" else "hip-v2"}/acceptance.json'))==18
 assert len(load(f'mmu-{arch}/acceptance.json'))==24
 assert all(x['fct_matched']and x['wire_matched']for x in load(f'holdout-{arch}/acceptance.json'))
 assert all(x['fct_matched']and x['wire_matched']for x in load(f'reserve-{arch}/acceptance.json'))
 joins=load(f'real-join-{arch}/acceptance.json')
 assert len(joins)==2 and all(len(v['modes'])==3 for v in joins.values())
 assert all(v['matched']and v['lead_comparisons']==624 for x in joins.values()for v in x['modes'].values())
assert len(load('tcp-mixed-o0-hip/acceptance.json'))==4
assert len(load('supplement-acceptance.json')['strong_queue_cuda'])==15
for arch in ['hip','cuda']:
 assert (r/f'lineage-flags-{arch}.log').read_text().count('accepted=1')==18
assert (r/'root-guard/rc').read_text().strip()=='1'
status={str(i):dict(status='passed',scope='see REPORT.md and raw receipts')for i in range(1,12)}
status['10']['note']='12 packet inputs plus 2 reused closed controls; additional lossy case has an explicit precision flag and lacks an accepted native trace; it is excluded from the passed count'
for i,reason in [(12,'No selected author-referenced switch state machine requiring real path/order change'),(13,'No selected TCP input requiring more than 32 active switch ports')]:status[str(i)]=dict(status='deferred',reason=reason)
status['extra_loss_holdout']=dict(status='flagged_not_accepted',gpu_rc={'hip':3,'cuda':3},flows=127,reason='one static root-order comparison encounters equal lossy lead codes; stock reference has no comparable completion/wire output; no precision claim')
(r/'acceptance.json').write_text(json.dumps(status,indent=2)+'\n')
lines=['本轮实验回执（2026-11-27）','',
'对应 `materials/TODOS.md` 的 11/27 统一清单。第 1–11 项的主要验收已补齐；第 12、13 项按其启动条件暂缓。额外预封存的 127 流有损样本触发精度标志且未取得可接受的原生参照，单列为未验收，不算精确通过。论文 TeX、图表与 TODOS 的勾选均未修改。','',
'本机根目录：`/home/cassius/Work/SpecDES/twins-paper-tcp-options/out/unified-review/`。主要原生计算和 HIP 编译在 a87；RX GPU 在本机；5090 GPU 与明确要求同机的 9950X 基线在 s50 原有独占锁下运行。所有正式三轮已经结束。源代码基于 `8ae29d7`；源快照和生成策略哈希随本回执保留。','',
'| 清单 | 结果和本机证据 |','| --- | --- |',
'| 1 | 两池、两模式、双后端共 8 臂，逐端口字段、多重数、同键内次序均一致；64,145 / 57,225 条发送。10,393 ns 和 212,638 ns 的指定端口均为 P 后 D。40 容量完整退出 0、39 容量截断退出 3；多记录、少记录和端口内换序均被拒绝。见 `pfc-{hip,cuda}/`、`pfc-checker-controls.json`、`cutoff-*/trace-cap-*`。 |',
'| 2 | 每后端 18 项截止／导出比较通过：配置、覆盖参数、批内换序、默认和排空，两个指定截止分别 0 / 1 完成、各 34 发送，IRN 14 字段一致。混合批保留健康、参考也停滞和重试世界的独立输出；另有 1/3 世界重试对照。相同对齐截止旧／新实现三轮输出一致，HIP 墙钟中位数 1.317 / 1.319 s，CUDA 0.965 / 0.965 s。见 `cutoff-*`、`mixed-*`、`rdma-mixed-hip`、`aligned-*`。 |',
'| 3 | RX 普通入口完整匹配 10,240 条六字段、末遍零标志；峰值 10.223 GiB。默认含重试 43.361 s，预分配足够容量 28.718 s。5090 同入口 4.451 s / 10.82 GiB，预分配 2.693 s；宽布局 20.23 GiB，仍可显式选择。固定小容量两端均非零拒绝。最新 O0 紧凑构建另过 4 项 128 主机 DCTCP/CUBIC、时间戳及双开对照：逐队列和逐主机定时器原序一致。混合批保留双关世界。见 `storage-hip-release`、`storage-cuda`、`tcp-mixed-o0-hip/acceptance.json`。 |',
'| 4 | 5090 的 15 个成功计时样本均匹配强 incast 的 320 FCT、31,396 次两端口发送且末遍零标志；固定小容量退出 3。旧整遍收集、立即、100 µs、默认自适应、预分配的三轮中位数分别 12.381 / 10.430 / 10.428 / 10.429 / 8.376 s，均为完整调用。RX 多热点探针自适应完整恢复 26.429 s；保留立即早停反复重试的 150 s 超时负例。见 `queue-*`、`supplement-acceptance.json`。 |',
'| 5 | 双后端各 18 格，固定及两组独立基数；顺序插入 UID、直接回溯、缓存冷／热及生产比较一致。2 项容量的尾环覆盖被识别，1,024 项容量末遍恢复；有损 lead 经真正 before 路径设置 undecided=1，丢失尾部设置 lost，正对照零标志。见 `lineage-flags-*.log`。 |',
'| 6 | 双后端各 24 个可达 MMU 状态，逐端口发送、FCT 及公共边界一致；另各 12 项 admission 319/320/321 B、pause 283/284/285 B 对照，与独立顺序和串行一致。真实有损 IRN 的串行／并行检查两输入均匹配冻结原生 FCT 与完整逐端口发送；ECN 输入 1,782 丢包、88,448 标记，26,399 并行 / 9,371 回退活跃槽。见 `mmu-*`、`admission-*`、`pause-*`、`irn-stream-*`、`supplement-acceptance.json`。 |',
'| 7 | 检查器能定位额外、缺失、重复身份及同刻换序。原 128 GPU 真 SimAI 每后端三臂各 9,920 回调、3,840 消息完全一致。新增两份 8 GPU 真 AllReduce，每后端三臂：各 5,377 回调、1,792 消息，每臂实际 624 次同 lead 比较。3,073 ns 为 timer 先，3,063 ns 为 receive 先；10 ns lead 相同。单槽视界在 85,696 ns / 85,491 ns，前 1 ns、边界、后 1 ns 的事件全部按排他边界执行。setup 相对序、父动作／ordinal、下一发送及身份均一致。见 `real-join-{hip,cuda}/acceptance.json`、`closed-order-*/acceptance.json`。 |',
'| 8 | 双后端普通复用 / incast：5,246,473 + 171,896 队列记录；混合链路和路由切换另 1,304 条。定向丢 DATA+FIN 与 FIN+ACK 两臂各 68,958 条，均与原生完整队列及六字段一致，另一连接仍完成。原生成器最后一次 Send 后立即 Close，数据发送已通过 ScheduleNow/+1 ps 排入，故可达的是尾数据携 FIN 的重传；未把不可达的独立空 FIN 重传当作已测。见 `tcp-close-acceptance.json`、`native-fin` 与 `fin-*`。 |',
'| 9 | 9950X 原生 / Unison32 合计 36 样本（各 18），5090 18 样本，每组各三轮；六设置 TwiNS 全部六字段与重传数相同，Unison 差异单列。记录每遍、累计循环和整进程；另 18 次一槽初始化观测报告路由和分配。CUBIC 的 46 ms 后仅占插桩循环 0.35%，无需新增活跃实体调度。见下表和 `figure-acceptance.json`、`figure-setup-positive`、`cubic-audit`。 |',
'| 10 | 封存 12 包级 + 2 已归档闭环控制，10 首批及 2 后揭晓包级两端均逐流／逐包匹配；四份封存 IRN 的正反批行序逐世界保持单跑完成集与逐端口完整记录。两闭环控制离线核对：重复 3,840 / 3,840、停滞 1,918 / 1,920，完成身份和父动作一致；不混入新的随机样本分母。原生秒浮点读入的 1 ns 首差与标准化输入单独保留。ECN 四条尾流身份／时间重新回到原生事件；MoE 两端各 6,496 条 live 消息匹配，录制重放 HPN 提前放行 565 条而 200G 为 0；稳定带宽对照排名保持。见 `holdout-*`、`reserve-*`、`closed-controls.json`、`case-replays/summary.json`。 |',
'| 11 | 同一冻结生成物：HIP hook 396 + 4,284 项匹配；两种策略各在 HIP/CUDA 普通入口复测尾段 RTO、incast 快恢复，队列／FCT 一致；与原三策略混合批双行序保持单跑输出。策略各自新增手写状态转移 0 行；通用适配另计，没有可靠人工计时就不编造工时。见 `policy-*`、`*-hip-hooks.log`、生成头 SHA-256 512610b9d95491f9421d60040f4d8554fd9e38553800d588b5d29d99e2b4a049。 |',
'| 12–13 | 没有已选定、必须触发新交换机状态的作者参照，也没有必须使用超过 32 个活跃 TCP 端口的实际输入。按清单暂缓；RDMA 宽端口不替代 TCP 证明。 |','',
'同机完整进程中位数（秒；GPU 含全部重试）：','',
'| 输入 / 双开 | ns-3 | Unison32 | TwiNS | ns-3/TwiNS | Unison 六字段差异 |','| --- | ---: | ---: | ---: | ---: | ---: |']
for key,v in fig.items():
 times=[v[e]['wall_s'][1]for e in ['ns3','unison32','twins']]
 diff=v['unison32']['observations'][0]['six_field_differences']
 lines.append(f'| {key} | {times[0]:.3f} | {times[1]:.3f} | {times[2]:.3f} | {times[0]/times[2]:.2f}× | {diff} |')
lines+=['',
'CUBIC/Short 固定 100 ms：双关完成 50,811/50,818，双开 50,816/50,818，ns-3 和 TwiNS 是同一未完成身份；TwiNS 返回 1 明确表示未全部完成。末遍精度标志均为零。不能把这六次部分完成与容量／未决错误混为一谈。Unison 固定 32 线程，本轮不声称其为双开设置的最快线程数。三轮范围、循环和重传详见 JSON。',
'',
'初始化观测仅推进一槽，计初始容量的一次建图／路由、设备分配和图实例化；不代替正式完整调用。整进程减循环包含各遍初始化、重试驱动及退出，不单称“路由”。普通 FatTree 在 GPU 有现成闭式路由，其拓扑／路由阶段极短；ns-3 的原生 PopulateRoutingTables 单列。',
'',
'HIP 编译限制：Clang 22 的优化紧凑 127-run 构建在 RX 大输入第 497 槽复现地址访问错误；分配成功、没有 OOM。改用常规 CMake 仅对此目标采用 O0 后，全量及 128 主机队列／定时器通过。保留 `hip-failure-diagnostic-source.patch` 与所有失败记录；没有把 O0 的性能当优化构建性能。CMake 配置和生成命令已检查，短前缀、策略适配和 O0 均进入对应目标。',
'',
'HPCC 可达性：初始无符号回绕由 unsigned-wrap 输入覆盖。初始预算非负时，串行 UpdateIngressAdmission 只有在新增端口共享量不超过当前 threshold 时才增加共享占用，而 threshold 不超过剩余预算；故合法准入不会把全局共享量推过预算下溢点。达到 admission/pause 的实际分支边界有真实包轨迹；没有伪造不可达的边界状态。',
'',
'额外有损封存样本（不并入 12+2）：127 流，种子 2026112707。两 GPU 均完成 127 流但返回 3：1 次根排序遇到相同有损 lead 编码；实际模型有 10,569 次丢包和 14,278 次 ECN 标记。未决标志禁止其计为精确通过。作者 stock 原生入口在本适配下没有可比的 FCT／发送输出；保留原始、动态库错误试跑及按参照要求排序的输入桥接，属于待解决的参照／输入边界，不能声称逐包匹配或参考停滞。见 `loss-holdout`、`loss-{hip,cuda}`、`loss-native-*`。已通过的真实有损 IRN 两输入和原 32 项回归保持各自分母。',
'',
'取数和复跑：实现与脚本见 `experiments/paper_unified/README.md`。本回执索引和 `acceptance.json` 区分标准验收、额外带标志样本及有条件暂缓。重要原始数据、失败记录、命令、构建回执和逐文件 SHA-256 存在本机；远程副本只在比对成功后清理。']
(r/'REPORT.md').write_text('\n'.join(lines)+'\n')
print('Wrote REPORT.md and acceptance.json')
