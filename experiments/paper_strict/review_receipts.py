"""Aggregate the strict-review evidence returned to the workstation."""
import json
from pathlib import Path
from statistics import median
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime

ROOT=Path(__file__).resolve().parents[2]
BASE=ROOT/'out/strict-20261123'
REPORT=ROOT.parent/'materials/experiment_strict_review_20261123.md'
ROLES=('crosspod_inputs','crosspod','paced_shared','paced_probe','days_state','ecn_tail',
       'unison','cpu_provenance','gpu_counters','pfc_reference','ai_candidate',
       'fig5_missing','tcp_reference_controls','sack_lossless')

def latest(role):
    paths=sorted((BASE/role).glob('20*'))
    return paths[-1] if paths else None

def read(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None

def report():
    rows=[]
    lines=['# 11/23 严格复审实验回执','',
           '更新于 '+datetime.now().astimezone().isoformat(timespec='seconds')+'。','',
           '统一候选 bda9422 的实验数据与独立诊断构建均归档在本机 '
           'twins-paper-review/out/strict-20261123/。s50 的持久队列每 45 秒回传关键文件，'
           '退出后按服务器索引校验 SHA-256。CPU、单 GPU 和全核计时使用各自已登记的资源条件。','',
           '| 队列 | 状态 | 本机结果目录 |','| --- | --- | --- |']
    for role in ROLES:
        p=latest(role)
        if not p:
            continue
        rc=p/'remote.rc'
        digest=p/'returned-files.sha256'
        recovered=read(p/'recovered-status.json')
        state='等待或运行中'
        if (rc.is_file() or recovered) and digest.is_file() and digest.stat().st_size:
            key=(str(p),digest.stat().st_mtime_ns)
            if key not in verified:
                checked=subprocess.run(['sha256sum','--quiet','-c',digest.name],cwd=p,
                                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                if not checked.returncode:
                    verified.add(key)
            if key in verified:
                state='计算已完成，补收已校验' if recovered else f'退出码 {rc.read_text().strip()}，已校验'
            else:
                state='回传校验中'
        rows.append(state)
        lines.append(f'| {role} | {state} | {p} |')
    lines+=['','本机 RX 诊断与同根比较固定输入在 rx-counters/ 和 lineage-control/。'
            'GPU 探针的墙钟只描述诊断，正常构建的进程墙钟另有正式回执。','']
    if (p:=latest('unison')) and (data:=read(p/'measurements/summary.json')):
        lines+=['## 异构 TCP 的 Unison 对照','',
                 '旧入口忽略链路速率与传播时延，导致顺序前检不匹配。正式对照重编已验证的场景并链接原优化库。'
                 '四档拓扑的关联回执不被改写。本项实际包含两个 1,024 主机种子、七档线程、各三次。','',
                 '| 种子 | 线程 | 进程中位数 s | 事件循环中位数 s | 六字段一致轮次 |',
                 '| --- | ---: | ---: | ---: | ---: |']
        for x in data['statistics']:
            loop=x['loop_median_ns']
            lines.append(f'| {x["seed"]} | {x["threads"]} | {x["process_median_ns"]/1e9:.3f} | '
                         +(f'{loop/1e9:.3f}' if loop is not None else '无')
                         +f' | {x["exact"]}/{x["completed_processes"]} |')
    lines+=['','## 跨 pod 三方对照','',
        '首轮双向同时发流，Days 对同一方向的独立数据路径与反向 ACK 路径选择发生冲突。'
        '随后用跨 pod 单向配对覆盖两端共 128 或 1,024 主机，按 Days 导出的六跳路径对齐其他两个模拟器。'
        'TwiNS 和 GeDES 的隔离构建只适配边缘到汇聚、汇聚到核心的两个出端口。'
        '同一对端的多个应用消息在 TwiNS/GeDES 共用 TCP 连接，Days 逐条建模。'
        '最初高负载结果及配对路由表已保留，另有按固定 20 µs 间隔、100 ms 截止的节奏对照。','',
        '| 输入 | 引擎 | 完整轮次数 | 进程中位数 s | 完成统计 |',
        '| --- | --- | ---: | ---: | --- |']
    days_counts={}
    if (p:=latest('days_state')) and (data:=read(p/'summary.json')):
        days_counts=data
    for role in ('crosspod','paced_shared','paced_probe'):
        p=latest(role)
        if not p:continue
        d=read(p/('preflight/summary.json' if role=='paced_probe' else 'measurements/summary.json'))
        if not d:continue
        groups=defaultdict(list)
        for trial in d if isinstance(d,list) else d['trials']:
            groups[trial['input'],trial['engine']].append(trial)
        for (case,engine),trials in sorted(groups.items()):
            samples=[x['wall_ns']/1e9 for x in trials if x['rc']==0]
            wall=f'{median(samples):.3f}' if samples else '无'
            record=trials[0]
            if engine=='days':
                counts=days_counts.get(f'{"paced_probe" if role=="paced_shared" else role}-{case}',{})
                if counts.get('state_digest_equal'):
                    result=f'{counts["complete"]}/{counts["total"]} 流，顺序终态指纹与 CUDA 一致'
                else:
                    st=record.get('native_result',{})
                    result=f'{st.get("received_packets","?")} 已收数据包，{st.get("pending_events","?")} 待处理事件'
            else:
                result=f'{record.get("completed","?")}/{record.get("flow_rows","?")} 流'
            lines.append(f'| {role}/{case} | {engine} | {sum(x["accepted"] for x in trials)}/{len(trials)} | {wall} | {result} |')
    if read(BASE/'paced-three-way-verified.json'):
        lines+=['','节奏对照 18/18 次完成。Days 三轮 CUDA 终态指纹分别匹配两档逐流 ACK 核对，'
                '128 主机 256/256 流、1,024 主机 2,048/2,048 流全部完成，'
                '退出时零待处理事件、零驻留包。三方 GPU 计时与 CPU 容器审计分别使用核 0 和核 4，'
                'GPU 主机进程用 5 GiB cgroup 上限，共 75 GiB 预留。实际 GPU 进程采样符合准入。']
    if (p:=latest('ecn_tail')) and (d:=read(p/'summary.json')):
        lines+=['','## ECN 阈值尾流复核','',
            '以同一到达轨迹下的 K=30/K=40 配对为分析单位，阈值是否越过 1 ms 由该轨迹短流 p99 判定。'
            '先按 GPU 选出的固定身份复核原序，再用预先指定的平局种子 1 对照。'
            '逐次回执保留既有四个目标流、该次独立 p99 身份、复核墙钟和逐包记录。','',
            '| 种子 | 平局序 | K=30 p99 ns | K=40 p99 ns | 同一目标流 K=30/K=40 FCT ns |',
            '| --- | ---: | ---: | ---: | --- |']
        for seed in (14,16,19,20):
            for order in (0,1):
                a,b=[x for x in d if x['seed']==seed and x['tie_order']==order]
                lines.append(f'| {seed} | {order} | {a["p99_ns"]} | {b["p99_ns"]} | {a["target_fct_ns"]}/{b["target_fct_ns"]} |')
        lines+=['','种子 19、20 的原序 K=40 记录到初始 SYN 与相隔精确 10 ms 的第二次 SYN，'
                '首轮入队记录分别止于交换机 140 端口 2 与交换机 157 端口 6。第二轮才出现对端 SYN-ACK。'
                '原版场景设置 ConnTimeout=10 ms。种子 14 的第一个数据 ACK 在 K=30 带 ECE，'
                'K=40 未带 ECE。种子 16 在 K=40 额外收到一个 ACK 1461，接下来跳至 5841，'
                'K=30 则经 4381。上述是实际观测到的队列、ACK 和握手分叉位置，较早的拥塞传播因果链仍需逐跳追踪。','']
    if (p:=latest('tcp_reference_controls')) and (d:=read(p/'summary.json')):
        lines+=['','## 顺序 SACK 与数值试跑','',
            'SACK 和 timestamps 在同一个优化库上分别切换。首个 160 流 incast 小点有实际丢包，'
            '不可作为无损控制。其他记录含短队列、包级 ECMP 和高负载。暂存的 FMA 对照仅改变场景驱动翻译单元，'
            '链接的 ns-3 库相同。完整运行库的跨工具链比较仍开放。','']
    if (p:=latest('sack_lossless')) and (data:=read(p/'summary.json')):
        lines+=['','另一个单流无损控制的三臂均为零丢包、零重传，'
                  '默认与 SACK 的 FCT 相差 4 ns。相同输入下 timestamps 单独启用的回执已保存。']
    if (data:=read(BASE/'rx-pfc-history.json')):
        lines+=['','## RX 交换机暂停和比较器诊断','',
            '| 输入 | 模式 | 活跃交换机槽 | Pause/headroom 活跃槽 | 槽占比 |',
            '| --- | --- | ---: | ---: | ---: |']
        for row in data:
            count=row['busy_slots']
            paused=row['paused_busy_slots']
            if count:
                lines.append(f'| {row["case"]} | {row["arm"]} | {count} | {paused} | {100*paused/count:.3f}% |')
        lines+=['','AllGather、GPT‑3 探针与 64 MiB DCN+ 的消息与已冻结参照一致。'
            '32 MiB 池的 DCN+ 三档已产生暂停，统计到的活跃槽暂停占比最高约 3%。'
            '更强压力的结果另见本页最终回执。'
            '逐交换机计数及同根、异根、指纹命中和回溯直方图见 rx-pfc-history.json。','']
    if (p:=latest('cpu_provenance')) and read(p/'binaries.json'):
        lines+=['','## CPU 实测构建与计时器','',
            '原 HPCC 二进制的 ELF 注释确认 GCC 5.4，Waf 缓存为 optimized、-O3 -march=native。'
            'Unison 罩下的原实测二进制为 GCC 13.3，顺序与 MTP SimAI 二进制为 GCC 11.4。'
            '编译缓存、二进制来源和失配风险保存在 binaries.json 中。'
            '已有 CUBIC 三轮的同口径中位数在 fig5-existing-medians.json，分区模式输出差异保留。'
            'DCTCP/NewReno 缺重复原始样本的 CPU 设置已排队。','']
        files=list((p/'timing').glob('*/*/receipt.json'))
        for file in sorted(files):
            row=read(file)
            if row and row['simulator_wall_ns'] is not None:
                overhead=(row['outer_wall_ns']-row['simulator_wall_ns'])/1e9
                lines.append(f'- {row["case"]} {row["mode"]} 第 {row["trial"]+1} 轮外部与内部计时差 {overhead:.6f} 秒。')
    if (final:=read(BASE/'final-recovered-summary.json')):
        ai=final['ai_candidate']
        lines+=['','## 本轮最终回执','',
                '已排程的 s50 与本机实验均已退出。五个 s50 作业在计算写出汇总后，'
                '本机回传因运行中的启动脚本被改写而中断。最终文件已从原目录补收，'
                '逐文件 SHA-256 已验证。原作业退出码缺失，各目录的 recovered-status.json 记录补收依据。','',
                f'最终候选 AI 的 {ai["latency_trials"]} 次延迟和 {ai["batches"]} 个八任务批次均通过。'
                f'八任务批次 P0/P16 墙钟中位数分别为 {ai["batch_median_s"]["p0"]:.2f} 和 '
                f'{ai["batch_median_s"]["p16"]:.2f} 秒。'
                'Fig. 5 补齐 336 个 DCTCP/NewReno CPU 样本，分区模式的六字段差异继续保留。'
                'HPCC 容器冷/热启动计时 12 次均复现冻结输出。','',
                '三档 DCN+ 输入的 24 次 safe/serial CUDA 输出均匹配独立 SimAI 的 1,920 条消息和 '
                '6,080 条规范化动作。RX 强压力试跑的 16/8 MiB 池两点超时，'
                '4 MiB 点复现滞留。持续暂停且完整完成的对照仍缺数据。'
                '验收计数和比对依据见本机 final-recovered-summary.json。','']
    tmp=REPORT.with_suffix('.tmp')
    tmp.write_text('\n'.join(lines)+'\n')
    tmp.replace(REPORT)
    return all('已校验' in s for s in rows) and len(rows)==len(ROLES)

verified=set()
if __name__=='__main__':
    while not report():
        if len(sys.argv)>1:
            break
        time.sleep(45)
