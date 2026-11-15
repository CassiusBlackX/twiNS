#!/usr/bin/env python3
"""One-shot reconciliation of experiment-owned Markdown. Never opens LaTeX files."""
from pathlib import Path
import datetime,hashlib,json,re,shutil
E=Path('/home/cassius/Work/SpecDES/twiNS/out/paper-evidence-20261116');M=Path('/home/cassius/Projects/TwiNS');stamp=datetime.datetime.now().strftime('%Y%m%d-%H%M%S');backup=E/'materials-before'/stamp;backup.mkdir(parents=True,exist_ok=True)
changed=[]
def write(name,text):
 p=M/name
 if p.exists():shutil.copy2(p,backup/name)
 p.write_text(text if text.endswith('\n') else text+'\n');changed.append(name)
old=(M/'experiment_results_20261116.md').read_text();table=re.search(r'\| 用例 \|.*?(?=\n\n)',old,re.S)[0]
write('experiment_results_20261116.md',f'''# TwiNS 本轮实验与实现结果（2026-11-16）

更新于 {datetime.datetime.now().astimezone().isoformat()}。本页区分已验收证据和仍在执行的队列，不改 LaTeX。Astra 可先采用 A2 三轮对照、A4 定位结果和 A6 反例/修复验证；A3 重复与整机吞吐尚未完成。

入口：[计划](experiment_plan_20261116.md)、[A2 成本与存储表](a2_cost_results_20261116.md)、[本机实时页](experiment_results_local_live_20261116.md)、[s50 实时页](experiment_results_live_20261116.md)、[A1 前提核查](a1_implementation_audit.md)。本机数据根 `SpecDES/twiNS/out/paper-evidence-20261116/`，下文所有相对数据路径以此为根。

## A1：日志、理论与版本

日志只打印实际 `C_obs/n_obs`、decisions 和底数，不再由运行后统计给出概率证书。HIP/CUDA 小负载构建、三引擎校准及固定底数输出/flags 复现通过。`TWINS_NO_FP` 关闭相等捷径，仍更新和携带 FP，不能把其差值称为完整指纹成本。默认精确 memo 使用完整身份/剩余计数核对；旧 opt-in HASH_MEMO/LSID 不纳入保证。理想执行、有限容量 flags/重跑、并发内存可见性和未耗尽身份空间仍须按审计中的契约陈述，测试不替代证明。

三条源码来源分开保存：s50 A3 使用最初 A1 日志修订版本（`source-a1.json`）；A2 最终所有 clean/profile/strict 二进制固定为 `48e0a3a`（`frozen-candidate.json`）；其后 `6bc5a7c` 为 UINT32_MAX run-count/prefix 的保守边界补丁，独立 HIP/CUDA 门禁排队中，**不能将已有时间改标为该候选结果**。既有测量历史长度远低于该 32 位边界。主工作树仍为 exact-audit，未合并 main、未发布 tag。

## A2：已完成的三轮数据

8 个设置 × clean/profile × 3 = 48 次测量已完成，全部所比对输出一致；64/128B 长历史 TCP 仍有 flags，36 次通过无 flag 验收，不能把 12 个带 flag 点当作保证成立时的性能。有效长历史点为 256B。

- 大 allgather：clean 中位数 22.359496631s，profile 26.867208593s，插桩倍率 1.202；GPT13B probe 为 96.123455550s / 117.975552878s，倍率 1.227。插桩测量不能替代干净性能。
- 两个 AI 用例抽样完整 RLE 的 length/runs 仅约 1.079 / 1.084。锁步/PFC 小例约 2.680 / 2.275。论文不能把所有收益归于巨大 RLE 压缩。
- 三组真实比较输入的 GPU 微基准均通过输出检查；分别计 FP 更新、含 FP 的 suffix 构造、精确 walk 的冷 memo/暖 memo、FP 捷径，每阶段每轮先 untimed warmup，再计三次。重建连续 tail 布局，不能代替生产缓存局部性，也不相加当墙钟。FP shortcut 的旧时间因包含 corpus 捕获钩子而排除；修正版另存 micro-isolated-result.json。旧 v1 因 FP 后缀范围错误而排除，保留作审计记录。
- 锁步输入仅 flow_size ×1/2/4/8，起始时刻不变；四个新顺序参照全部完成并回传。clean/profile 八次运行全部 FCT 相同、零 flags。尾节点累计分配随倍率增长，ring 驻留上限固定，必须区分累计分配和同时占用。
- 指纹与直接遍历对照仍在后续队列，每例一小时上限，超时记录为删失而非“错误”或精确完成时间。不会用旧 no-chain、不同轨迹的比值解释机制成本。

表格、计数解释、存储修正及直方图：`reports/a2-derived.json`、`reports/a2-paper-tables.md`。图及 provenance：`reports/figures/`、`reports/figure-provenance.json`；生成器为隔离 worktree 的 `experiments/paper_evidence/derive_a2.py`。inline 包含 FP 字段；tail、memo、其他显式状态分列，剖析辅助 64MiB 单列扣除，TCP switch last_chain 根据实际分配补计。tracked highwater 不含 driver/runtime；设备 VRAM 抽样含桌面背景。

## A4：容量与 timer 表示

24 次容量补验全部在本机。比较前六个 TCP CSV 字段（src,dst,flow_size,start,established,completed）；完整文件 SHA-256 及额外 hash/retx 全列比较另存，不能解读为全包 trace 一致。所有容量尝试无 overflow。

{table}

两个 FIN 案例的 timer flags 为 27/81，g075 为 1，到 512B 仍存在。独立默认关闭的 `TWINS_TCP_TIMER_ORDER` 在 128B 记录同一主机 timer 的创建顺序：三个定位例均 FCT 相同、零 flags，无须扩大帧内历史；每连接增加六个 64 位键、每主机增加一个 64 位计数器。主集/网格广泛回归仍排队，因此只将三例局部验证记为完成，不把实验性表示称为发布后的默认能力。

默认自动回退仍到 128B，256/512B 为显式额外构建。`local/residuals/`、`local/residuals-timer/` 保存全部命令/墙钟/flags/行数/哈希，累计成本不能只取末次。新 inventory 有 1174 个 suite entries、1158 个保守配置键、76 个缺参照条目；这些是清单计数，**不是全局去重的成功场景数**。新的逐配置 default/128B 执行正在 `local/unified/` 写入，每尝试及累计墙钟保留；历史缺失 pass 仍为未知，不能反推。

## A6：admission 反例与修复边界

已确认一个原 safe predicate 漏检 admission 的可达四端口 mixed-rate 拓扑：B=1048576、各端口 headroom=255/343125/343125/343125、R=16384、Q=2562；一条 1Gbps/680ns 低 headroom 链路及三条 100Gbps/9150ns 链路。真实 CC3 模型 wire size=1094B；1064B 只用于另列的算术 fixture。

原 safe/window 完成 1/1（FCT 29242ns、drops=0），同构建 serial 和重新构建的可移植顺序 oracle 均 0/1、drops=1，首个 admission drop 在 9432ns；原程序两种模式均无 history flag。这说明 history flags 不能检查 handler fidelity。候选新 guard 使 safe/serial/window 均为 0/1、drops=1、rc=2，**同样 stranded 不能写成全部完成**。

新充分条件在原 PFC/unsigned-wrap/signed-room 条件之外，对每 ingress 取 `U=max(ing+A-RESERVE,0)`，并要求 `U<th_min`；若 `A>headroom_cap`，还需 `A+U<=th_min`。`A` 为该槽的 ingress byte 上界。host 端拒绝零 payload、超过 16 位 wire size 和不可表示的 headroom。精确保留参照 unsigned wrap，而非将负缓冲余量擅自钳为零。

数值验证：旧 578 组、补 admission 后 579 组，提取生产算术编译，UBSan 无报告；合成边界状态与网络可达状态分开标记。网络验证：6 场景 × safe/serial/gwin4/gwin4+serial = 24 组，FCT、公共 slot 边界的 shared/ing/hdrm/egr/paused/threshold 均相同、零状态差异、零 history flags；记录的 TX 十个字段多重集和逐链路顺序 24/24 一致。观察器开/关不改变 FCT。比较仅针对共同边界和参照停止前的状态，不声称完整内部协议状态。

PFC 例实际发生 18 次 pause 和 18 次 resume；NVLink idle 小拓扑实际触发 default global32 的 7 次聚合窗口，以及 gwin4 的 38 次，forced-serial 对照实际回退。未触发的旧 v1/v2 不计覆盖。证据在 `local/mmu/boundaries_v3/`（逐次 result.json、整数状态、`tx-validation.json`）。最初远端 oracle 因本机无 AVX-512 触发 SIGILL；已从同源关闭 native 指令优化重建，后续验证使用可移植 oracle，不把 ISA 问题算作仿真差异。

## 仍在执行及发布边界

s50 正在完成八负载 SimAI/P0/P16 三轮轮换测量，按波动 >10% 或持续干扰追加两轮；其后 9 次消融、独立 CUDA 候选门禁和真实整机吞吐。CPU 吞吐目标并发 1/2/4/8 各三批，总预留不超过 70GiB；每作业 cap 从 A3 三轮已验收 cgroup peak 冻结为 ceil(1.10×最大峰值+512MiB)，至少 1GiB。GPU P0/P16 各顺序八作业三批。makespan 含启动和 stub，provenance 和校验单列在计时外。驱动已用真实子进程自检并发、内存准入和计时边界，调度自检不算科学测量。

本机依次补统一 manifest、timer 广泛回归、strict 对照，独立 HIP 边界候选检查另排队。所有后台任务都按 result.json 更新实时页，启动不算完成。A5 目前是隔离候选和数据映射；完整匿名工件、最终 release/tag、投稿检查仍未完成。诊断工作流见 [diagnosis_workflows_20261116.md](diagnosis_workflows_20261116.md)。

## 本机保存策略

重要结果、纳秒计时、命令、版本/输入/参照/二进制哈希、资源采样、校验摘要、统计图表和必要的反例片段保存在本机。s50 仅拉取 export/ 与构建 receipts，逐文件 SHA-256 校验结果写入 `receipts/local-archive-verification.json`；普通长 trace、临时排序缓存和可生成的大 DAG 不回传。成功 bulk 输出经完整验收后清理，失败摘要保留。历史目录与原未提交文件不删除。
''')
shutil.copy2(E/'reports/a2-paper-tables.md',M/'a2_cost_results_20261116.md');changed.append('a2_cost_results_20261116.md')
p=M/'TODOS.md';s=p.read_text();s=s.replace('**A6 已复现一个原 safe predicate 漏检 ingress admission 的可达小拓扑；候选修复通过 safe/serial/window 对照，但顺序 oracle 与公共边界状态验证尚在进行。**','**A6 的顺序 oracle、24 组公共边界 MMU 状态及 TX 顺序对照已通过；A2 的 48 次 clean/profile 和增长曲线已完成。strict 对照、A3 重复、统一 manifest、吞吐及最终候选门禁仍在执行。**')
s=s.replace('状态：未完成；对应旧 R-5 精确性代价分解。现有 safe-slot/window 消融已经完成，可直接复用。','状态：部分完成。固定六类/八设置的 48 次 clean/profile、三组真实输入微基准、四点增长曲线及存储/分布表已完成，见 [a2_cost_results_20261116.md](a2_cost_results_20261116.md)。strict 对照仍排队，不能将整项标完。早期高扰动插桩单列排除。')
s=s.replace('状态：42 个同机基线和 `P=0/P=16` 已验收，但每设置只有一个正式计时，不能据此计算波动或置信区间。','状态：旧 42 个同机基线与 P0/P16 单次结果保留；新固定八负载三轮交错测量正在 s50 执行，已完成点见实时页。重复未齐前不出波动结论。')
s=s.replace('完整统一表和新的 timer/ordinal 表示仍未完成。','实验性 timer 创建顺序表示在三个定位例通过，128B 即零 flags、FCT 一致；广泛回归和统一表仍在执行，默认回退规则未改变。')
s=s.replace('状态：已运行原 578 组及修复候选 579 组算术 fixtures，复现四端口 mixed-rate admission 反例。隔离候选补充 admission guard，safe/serial/window 均回到同样丢包/未完成状态；顺序 oracle 和公共 slot 边界 MMU 状态验收仍在进行，不能只凭原 PFC 阈值条件声称 handler fidelity。','状态：本机边界证据完成：578/579 数值 fixtures、可达 admission 反例、可移植顺序 oracle、6×4 公共 slot 边界 MMU 状态和 TX 字段/链路顺序均已验证。候选增加 admission guard；PFC 与多槽均实际触发。详见结果页，匹配 stranded 不等于全部完成；CUDA 新候选兼容门禁仍属 A5 待验收。')
s=s.replace('状态：未重测。当前论文已明确单次顺序参照加速；此实验进一步说明资源效率。','状态：已排入 s50 延迟/候选门禁之后，尚无正式批次结果。固定八作业、CPU 1/2/4/8×3、P0/P16×3，总 cap 70GiB；真实 makespan 将与单次延迟分开。')
for text in ['按论文理想执行定义核对比较器的分支、memo、尾链失效、并发交错和内部重跑；检查实际实现满足“首次错误指纹相等之前控制流一致”的前提。固定底数用于复现；理论抽样保证针对独立随机选择的底数。','预先固定代表性场景：普通 TCP、长流/长历史 TCP、PFC RDMA、集合通信及 NVLink 密集训练。','先固定约 6--8 个代表负载，包含三个完整 128-GPU 训练迭代及代表性 safe-slot/window 消融；覆盖小集合通信、长集合通信、完整 128-GPU 训练、512/1024-GPU 训练及 islands 收益不同的场景；选择依据写入计划。','预先定义 PFC 阈值、reserve/headroom、暂停/恢复以及 unsigned wrap 附近的边界用例；覆盖正常 safe、持续 serial、两者频繁切换和 multi-slot window 回退。','对照顺序 HPCC/SimAI 检查逐事件/逐流输出和 MMU 状态，记录 safe 比例、回退次数、时间与最终 flags。','同时核对 admission 的包长/headroom 分支、端口计数器的整数范围及容量前提；若发现未覆盖的参数区域，先明确契约或修正 predicate，再重跑相应验证。不要只凭现有宽交换机全部 safe 的测量推广到任意参数。','验收：少量可复现的边界样例与明确的不变量说明；不新增无关协议或规模实验。']:
 s=s.replace('- [ ] '+text,'- [x] '+text)
# The A6 event/state checkbox refers only to the recorded fields and common boundaries above.
write('TODOS.md',s)
p=M/'paper_data_sources.md';s=p.read_text();head,sep,rest=s.partition('\n---\n');head=head.replace('顺序参照/公共边界 MMU 状态仍待验收。','可移植顺序参照已验收，6×4 公共 slot 边界 MMU 和 TX 字段/链路顺序 24/24 相同；PFC 和多槽实际触发。')
head+='\n- A2 最终三轮固定 `48e0a3a`，表见 [a2_cost_results_20261116.md](a2_cost_results_20261116.md)，原始 `local/profile-final/`、`local/corpus/*/micro-final-result.json`、`local/growth/`；图与来源映射在 `reports/figure-provenance.json`。三轮已齐，strict 尚待；短容量带 flag 点不能作为无 flag 性能。\n- A4 timer 表示默认关闭；三个定位例已通过，主集/网格回归未齐。清单新增 what-if/sweep 后是 1174 entries，不将其称为去重成功数。\n- s50 重要 export 已建立逐文件 SHA-256 本机验收索引，普通大 trace 不回传。早期 CPU started.json 错记了 TwiNS 配置哈希；实际命令使用 CPU 配置，现于 `s50/export/provenance-supplement/` 保存真实 CPU 输入副本和补充哈希，捕获时间和局限如实记录。\n'
write('paper_data_sources.md',head+sep+rest)
p=M/'for_paper.md';s=p.read_text();prefix='''> 本轮实验更新：A2 三轮成本/存储与增长曲线、A4 容量/timer 定位、A6 admission 反例及 24 组状态/发送顺序验证已取得本机证据，见 [experiment_results_20261116.md](experiment_results_20261116.md) 和 [a2_cost_results_20261116.md](a2_cost_results_20261116.md)。A3 重复和整机吞吐仍运行。旧数值保留其真实版本，不能把 A1 时间重标为 MMU 修复后的候选；也不能把 RLE 解释成两个 AI 用例的主要压缩来源。

'''
if not s.startswith(prefix):write('for_paper.md',prefix+s)
p=M/'a1_implementation_audit.md';s=p.read_text();s+='''

## 2026-11-16 后续边界核查

A2 最终证据冻结在48e0a3a。其后的6bc5a7c补丁使虚拟 prefix push 的 UINT32_MAX 次数保持饱和，并在 prefix 将越过饱和 run 时返回未知，让完整比较器处理；新增有限 run 的正对照。该极限路径的独立 HIP/CUDA 门禁仍排队，不能将旧二进制数据改称新候选数据。

理想执行必须保持与实现一致的容量、flag 和内部 retry 策略，仅把 FP 相等判断换成完整逻辑 leads 的精确相等。若论文的理想执行同时删除容量限制/重跑，需要另给耦合论证；有限 ring 的 TWINS_NO_FP 程序也不能直接冒充该理想执行。源码核查已完成，GPU memory model 的形式化证明与每运行预算型概率证书未实现。
''';write('a1_implementation_audit.md',s)
p=M/'experiment_plan_20261116.md';s=p.read_text().replace('共享机器无法独占时','共享机器无法独占时');s=s.replace('追加一次；不因低利用率任务无限等待','追加两轮并保留原样本；连续三个 5s 采样/区间触发持续干扰判据。P16 按绑定的 0–16 逻辑核集合记录，不能宣称 SMT 独占；不因低利用率任务无限等待');s=s.replace('CPU 目标并发 1/2/4/8，各三轮；','CPU 目标并发 1/2/4/8，各三轮；每作业 cap 在吞吐开始前由 A3 已验收 cgroup peak 冻结为 ceil(1.10×最大峰值+512MiB)，至少 1GiB；');s=s.replace('检查开销另列范围。','provenance 在计时前、验证在整批结束后，均单列在计时之外。中断批次保留并整批重跑；清理仅匹配当前用户/本次 campaign labels。')
write('experiment_plan_20261116.md',s)
(E/'reports/materials-update-20261116.json').write_text(json.dumps(dict(utc=stamp,files={n:hashlib.sha256((M/n).read_bytes()).hexdigest() for n in changed},backup=str(backup),latex_modified_by_this_script=False),indent=2)+'\n')
print('Updated:',', '.join(changed))
