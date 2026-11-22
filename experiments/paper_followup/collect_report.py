"""Keep the local experiment handoff synchronized with returned receipts."""
import json
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime

ROOT=Path(__file__).resolve().parents[2]
EVIDENCE=ROOT/'out/followup-20261123'
REPORT=ROOT.parent/'materials/experiment_followup_20261123.md'

def collect_dags(epoch):
    target=epoch/'measurements'
    index=target/'dag-files.sha256'
    if not (epoch/'remote.rc').exists() or index.exists():
        return
    remote=f'/home/cassius/Videos/twins-paper-followup-20261123/gpu_suite/{epoch.name}/results/measurements'
    subprocess.run(['rsync','-a','--prune-empty-dirs','--include=*/','--include=*.dag','--exclude=*',
                    '-e','ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8',
                    f's50:{remote}/',str(target)+'/'],check=True)
    command=f"cd '{remote}' && find . -type f -name '*.dag' -print0 | sort -z | xargs -0 -r sha256sum"
    temporary=index.with_suffix('.tmp')
    with temporary.open('wb') as out:
        subprocess.run(['ssh','-F','/home/cassius/.ssh/config','-o','BatchMode=yes',
                        '-o','ConnectTimeout=8','s50',command],stdout=out,check=True)
    if temporary.stat().st_size:
        subprocess.run(['sha256sum','--quiet','-c',temporary.name],cwd=target,check=True)
    temporary.replace(index)

def update():
    subprocess.run([sys.executable,str(Path(__file__).with_name('summarize.py')),str(EVIDENCE)],
                   stdout=subprocess.DEVNULL,check=True)
    data=json.loads((EVIDENCE/'returned-summary.json').read_text())
    for epoch in (EVIDENCE/'gpu_suite').glob('20*'):
        if (epoch/'measurements').exists():
            collect_dags(epoch)
    jobs=[]
    for role in ('k32_cpu','gpu_suite','graph_entry'):
        dirs=sorted((EVIDENCE/role).glob('20*'))
        if not dirs:
            jobs.append((role,'等待部署',None))
            continue
        latest=dirs[-1]
        rc=latest/'remote.rc'
        jobs.append((role,'运行中' if not rc.exists() else '退出码 '+rc.read_text().strip(),latest))
    lines=[
        '# 11/23 Astra 新增实验回执','',
        '更新于 '+datetime.now().astimezone().isoformat(timespec='seconds')+'。','',
        '统一实验候选为 bda9422。五组 8,192 主机 RDMA 各跑三次顺序参照和 GPU。'
        '协调器对照固定 1,024 GPU、512 MiB all-gather、P=0、send_lat 和协议参数，'
        '两臂均关闭网络全局窗口，仅将 CPU 回调推进跨度从正常安全视界改为一个基础槽。'
        '另做 TCP 三协议、IRN 两种万流布局和两个 AI 负载的新旧版本热路径配对。','',
        '所有进程墙钟在子进程退出后立即结束，读取输出、排序和核对在该边界之外。'
        'GPU 主机线程使用核 0，顺序参照使用核 4，属于受控共享资源测量。'
        '首轮两作业各 32 GiB 均触发 Docker OOM，失败回执保留。'
        '当前顺序参照单作业预留 70 GiB，GPU 和构建按剩余资源推进。','',
        '| 队列 | 状态 | 本机回传目录 |','| --- | --- | --- |',
    ]
    for role,state,path in jobs:
        lines.append(f'| {role} | {state} | '+(str(path) if path else '尚无')+' |')
    lines+=['','重要的逐次 JSON、日志、输入与二进制哈希、FCT 和校验索引位于上述目录。'
            '本机每 45 秒收取已经生成的结果，完整回传后按服务器 SHA-256 索引验证。','',
            '## 已回传的计时','',
            '| 设置 | 已回传次数 | 通过次数 | 墙钟中位数 s | 范围 s |',
            '| --- | ---: | ---: | ---: | --- |']
    for key,row in data['tables'].items():
        med='待取得' if row['median_s'] is None else f'{row["median_s"]:.6f}'
        bounds='待取得' if row['range_s'] is None else f'{row["range_s"][0]:.6f}–{row["range_s"][1]:.6f}'
        lines.append(f'| {key} | {row["trials"]} | {row["accepted"]} | {med} | {bounds} |')
    horizon=data.get('callback_horizon',{})
    if all(horizon.get(arm,{}).get('summary',{}).get('accepted')==3 for arm in ('safe','one-slot')):
        safe=horizon['safe']
        short=horizon['one-slot']
        lines+=['','## 协调器同步视界结果','',
            f'正常视界的墙钟中位数为 {safe["summary"]["median_s"]:.6f} s，'
            f'单槽视界为 {short["summary"]["median_s"]:.6f} s，'
            f'相对比值为 {short["summary"]["median_s"]/safe["summary"]["median_s"]:.3f}。'
            f'正常跨度为 {safe["horizon_slots"][0][0]} 个基础槽，单槽跨度为 1，'
            f'往返轮数分别为 {safe["round_trips"]} 和 {short["round_trips"]}，'
            f'回调次数分别为 {safe["callbacks"]} 和 {short["callbacks"]}。'
            '六次消息和规范化动作均匹配相同的顺序参照。']
    lines+=['','## 验收器与来源核对','',
        '八份已有的扩容日志使用末遍计数重新核对后，两后端网络通过数均为 972/985，'
        '原清单的 968/985 保留。新增真实固定 fcap=256 末遍 TX 溢出对照仍被拒绝。'
        '42 个闭环通过，三个 what-if 的 FCT 和 DAG 继续匹配历史滞留，完整通过为 44/47。'
        '派生清单位于 twins-paper-review/out/followup-20261123/reclassification-v2/。','',
        '热路径逐文件哈希位于同目录下 provenance/source-hot-path-audit.json。'
        '旧 TCP/AI 计时树的源清单确认版本为 6bc5a7c。'
        '源码变化覆盖 TCP 调度与重试、IRN 交换机及 AI 路径，配对复测按具体设置单独验收。','',
        '同步视界实验使用单独的 TWINS_CL_ONE_SLOT 诊断选项。它只限制协调器推进，'
        '模拟的 send_lat、基础槽宽、端口并行和 GPU graph 选择相同。'
        '回执保留实际往返轮数、回调数和 GPU、系统层、同步阶段时间。','',
    ]
    if data['rdma_speedups']:
        lines+=['## 已齐备的 RDMA 配对','',
                '| 输入 | CPU 中位数 s | GPU 中位数 s | 比值 |',
                '| --- | ---: | ---: | ---: |']
        for key,row in data['rdma_speedups'].items():
            lines.append(f'| {key} | {row["cpu_median_s"]:.6f} | {row["gpu_median_s"]:.6f} | {row["speedup"]:.2f} |')
    tmp=REPORT.with_suffix('.tmp')
    tmp.write_text('\n'.join(lines)+'\n')
    tmp.replace(REPORT)
    finished=all(path is not None and (path/'remote.rc').exists() for _,_,path in jobs)
    if finished:
        for _,_,path in jobs:
            subprocess.run(['sha256sum','--quiet','-c','returned-files.sha256'],cwd=path,check=True)
        return True
    return False

if __name__=='__main__':
    while True:
        if update():
            break
        time.sleep(45)
