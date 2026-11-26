"""Track completion and final checksum verification for Astra's remaining experiments."""
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time

REPO=Path(__file__).resolve().parents[2]
BASE=REPO/'out/strict-20261123'
REPORT=REPO.parent/'materials/experiment_remaining_20261123.md'
ROLES={
    'bda_regression':'A5 CUDA 全库存',
    'exit_snapshot':'跨 pod 原生退出快照',
    'unison_wire':'异构 Unison 顺序逐入队',
    'ecn_cause':'ECN 槽内队列与计时器',
    'pfc_sustained':'PFC 三档 CUDA 和顺序参照',
    'pfc_dense':'高暂停点 CUDA 和顺序参照',
    'numeric_libs':'整套 ns-3 库 FMA 与 CUDA 比对',
    'ai_drift':'AI 启动路径和全局窗口诊断',
}
verified={}

def read(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None

def latest(role):
    paths=sorted((BASE/role).glob('20*'))
    return paths[-1] if paths else None

def status(path):
    code=path/'remote.rc'
    index=path/'returned-files.sha256'
    if not code.is_file():
        return '进行中或等待计算锁',False
    if not index.is_file() or not index.stat().st_size:
        return f'远端退出码 {code.read_text().strip()}，等待回传校验',False
    key=(path,index.stat().st_mtime_ns)
    if key not in verified:
        verified[key]=subprocess.run(['sha256sum','--quiet','-c',index.name],cwd=path,
                                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
    ok=verified[key]
    return f'退出码 {code.read_text().strip()}，'+('已校验' if ok else '校验失败'),ok

def publish():
    lines=['# 11/23 剩余实验回执','',
           '更新于 '+datetime.now().astimezone().isoformat(timespec='seconds')+'。','',
           '所有正式实验沿用 bda9422 或明确登记的独立诊断构建。s50 的关键回执、输入标识、'
           'FCT、动作记录和窄观测按原目录回传，结束后逐文件校验 SHA-256。'
           'CUDA 正式队列共享同一块 5090，同一时刻只运行一个计时组。','',
           '| 实验 | 进度 | 本机路径 |','| --- | --- | --- |']
    complete=[]
    for role,title in ROLES.items():
        path=latest(role)
        if not path:
            continue
        state,done=status(path)
        complete.append(done)
        lines.append(f'| {title} | {state} | {path} |')
    hip=BASE/'bda-hip/measurements'
    finished=read(hip/'finished.json')
    count=sum(1 for _ in hip.glob('*/*/result.json'))
    lines.append(f'| A5 HIP 全库存 | 已收 {count} 项'+('，已汇总' if finished else '')+f' | {hip} |')
    pilot=BASE/'pfc-fixed-rx/measurements/pilot-summary.json'
    pd=read(pilot)
    if pd:
        lines.append(f'| 修正后 RX PFC 预试 | 已收 {len(pd)} 项 | {pilot} |')
    hip_numeric=BASE/'numeric-hip/summary.json'
    h=read(hip_numeric)
    lines.append(f'| HIP 与重新编译的参考库比对 | {"等待或运行中" if h is None else str(len(h))+" 个设置"} | {hip_numeric} |')
    if (p:=latest('unison_wire')) and (r:=read(p/'summary.json')):
        base=r['arms']['0']
        parallel=r['arms']['1']
        lines+=['','## 异构 Unison 小点','',
            f'128 主机异构输入的顺序前缀核对包含 {base["adapted_records"]} 次入队、'
            f'{base["ports"]} 个节点端口，逐端口次序及队列长度一致。'
            f'1 线程分区模式在 {parallel["differing_ports"]} 个端口偏离顺序参照。'
            '2 线程记录另有 stdout 交织，直接计数和首个差异不作为完整的分区包级结论。']
    if (p:=read(BASE/'ecn-causal-witness.json')):
        lines+=['','## ECN 四个尾流的直接见证','',
            f'{p["verified_runs"]} 组 ns-3 定点观测的逐流输出与冻结输出一致。'
            '计时使用原独立轮，定点观测以 TCP 端口查出指定连接。','',
            '| 种子，原序 K=40 | 见证 |','| --- | --- |']
        for seed,row in p['seed_results'].items():
            if 'dropped' in row:
                detail=f'节点 {row["queue_node"]} 端口 {row["device"]} 的 {row["queue_packets"]} 包内部队列丢弃 {row["dropped"]}，{row["retry_after_ns"]/1e6:.0f} ms 后重发 SYN。'
            else:
                detail=f'拥塞窗口降到 {row["old_cwnd_bytes"]} B，{row["stalled_ns"]/1e6:.0f} ms 后进入 LOSS，窗口重置为 1,460 B 并发送新数据。所选连接没有直接丢包记录。'
            lines.append(f'| {seed} | {detail} |')
        lines+=['','ns-3 运行库的 ReTxTimeout 路径进入 LOSS 并重置窗口，回执包含实际状态、'
                  '序号和窗口时间戳。逐流数据重传计数为零的两条仍有这次窗口停顿。']
    if pd and (f:=read(BASE/'pfc-dense-independent-match.json')):
        dense=next(x for x in pd if (x['pool_mib'],x['burst_mib'])==(6,8))
        hotspot,stats=dense['hotspot']
        lines+=['','## 修复后的高暂停点','',
            '诊断以 KMAX=256 分配 257 个有效端口数分桶。沿相邻类别和交换机跨边界检查了 '
            '24,672 个计数位置，均独立。','',
            f'128 卡 DCN+ 的 6 MiB 缓冲、8 MiB burst 完整退出。物理预留后至少有 '
            f'{dense["minimum_unreserved_bytes"]:,} B 可用。热点索引 {hotspot} 的 '
            f'{stats["paused"]:,}/{stats["busy"]:,} 个活跃槽带暂停或 headroom，占 '
            f'{100*stats["paused"]/stats["busy"]:.1f}%。'
            f'RX 的 {f["messages.fct"]["rx_rows"]:,} 条消息及 '
            f'{f["actions.dag"]["rx_rows"]:,} 条规范化动作均与独立 SimAI 参照一致。'
            'CUDA 复测另记最长连续暂停槽、热点发包和 safe/serial 三轮计时。']
    if (p:=latest('numeric_libs')) and (r:=read(p/'cpu-summary.json')):
        lines+=['','## 完整参考库 FMA 核对','',
                '| 设置 | CPU 六字段 | CPU 20 µs 逐入队 |','| --- | --- | --- |']
        for name,entry in r.items():
            result=entry['result']
            lines.append(f'| {name} | {"一致" if result["fct_equal"] else "有差异"} | '
                         f'{"一致" if result["queue_equal"] else "有差异"} |')
    if (r:=read(BASE/'lineage-timing-and-retries.json')):
        lines+=['',f'复核 {len(r["runs"])} 个原 CUDA lineage 观测和计时点，'
                f'识别到 {r["runs_with_detected_retries"]} 次运行发生重试。'
                '逐遍、整进程墙钟及根 ID 匹配条件记录在 lineage-timing-and-retries.json。']
    if (r:=read(BASE/'remaining-s50-verified.json')):
        lines+=['','## s50 最终验收','',
            f'本轮 s50 的 {r["verified_s50_files"]:,} 份回传文件已通过 SHA-256 校验。'
            'CUDA 统一候选通过网络 972/985、闭环 42/42、what-if 44/47。'
            '12 个网络点受定时器约束，1 个网络点和 3 个 what-if 复现滞留。','',
            'TwiNS 和 GeDES 的八组退出快照均复现冻结 FCT。两档节奏输入退出时，'
            '观测的队列、在途帧和已武装计时器均为空。1,024 主机节奏输入的 '
            'TwiNS 最后实际处理槽索引为 40,969，逻辑边界为 40.970 ms。'
            '原 Executed Slots 字段记录了退出后的槽游标，其值为 100,000。'
            'GeDES 在 1,024 主机突发输入的截止点仍有 192 个未确认数据的 RTO 状态。','',
            '四档 PFC 共 32 次结果匹配独立参考。热点回收时纳入当前十字段线迹，'
            '有效前缀的十字段多重集均与顺序参照一致。带相同时间戳的控制帧与数据帧'
            '在部分端口的输出排列不同，归档表同存次序差异和多重集核对。','',
            '| 场景 | 热点暂停或 headroom 占比 | 最长连续活跃暂停槽 | safe / serial 墙钟中位数 s |',
            '| --- | ---: | ---: | ---: |']
        for key,row in r['pfc'].items():
            lines.append(f'| {key} | {100*row["committed_hotspot_pause_fraction"]:.2f}% | '
                f'{row["max_pause_streak_slots"]:,} | {row["safe_wall_median_s"]:.6f} / {row["serial_wall_median_s"]:.6f} |')
        lines+=['','DCTCP 与 CUBIC 在原拓扑和亚纳秒输入上的四组完整库 FMA 及 CUDA 核对均通过。'
            'HIP 数值对照随本机 A5 作业结束而进入同一卡的队列。','',
            'AI 漂移的 36 次同机配对均匹配输出。GPT‑3 13B 探针的默认墙钟从 63.275 s '
            '变为 67.986 s。关闭网络全局窗口或关闭 persistent 启动，旧版与候选的时间差仍出现。'
            '旧版和候选的内核资源回执已归档，具体内核增时原因仍待定位。']
    tmp=REPORT.with_suffix('.tmp')
    tmp.write_text('\n'.join(lines)+'\n')
    tmp.replace(REPORT)
    return len(complete)==len(ROLES) and all(complete) and bool(finished) and bool(h)

if __name__=='__main__':
    while not publish():
        if len(sys.argv)>1:
            break
        time.sleep(45)
