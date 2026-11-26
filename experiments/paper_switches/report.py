"""Verify and summarize the local 11/27 switch extension archive."""
import argparse
import json
from pathlib import Path
from statistics import median

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("archive", type=Path)
args = parser.parse_args()
root = args.archive


def get(folder):
    return json.loads((root / folder / "comparison.json").read_text())


def grouping(records, key):
    groups = {}
    for row in records:
        groups.setdefault(key(row), []).append(row)
    assert all(len(rows) == 3 for rows in groups.values())
    return groups


def timing(records):
    values = sorted(row["wall_s"] for row in records)
    return f"{median(values):.3f} [{values[0]:.3f}, {values[-1]:.3f}]"


tcp = {}
cw = {}
for backend in ("hip", "cuda"):
    trace = get(f"tcp-{backend}-confirm-trace")
    assert len(trace) == 8
    for row in trace:
        assert all(row[k] for k in ("six_field_match", "zero_flags", "queue_match", "route_match"))
        if row["case"].startswith(("48-", "64-")):
            assert row["active_high_ports"] and row["high_same_time_contention"] and row["high_max_depth"] > 1
    completed = get(f"tcp-{backend}-completion")
    assert len(completed) == 2
    assert all(row["rows"] == row["complete"] == 336 and row["six_field_match"] and
               row["queue_match"] and row["route_match"] and row["zero_flags"] for row in completed)
    samples = get(f"tcp-{backend}-wall")
    assert len(samples) == 24 and all(row["six_field_match"] and row["zero_flags"] for row in samples)
    tcp[backend] = dict(trace=trace, completion=completed, timing=samples)
    trace = get(f"cw-{backend}-confirm")
    samples = get(f"cw-{backend}-wall")
    assert len(trace) == 4 and len(samples) == 10
    assert all(row["matched"] for row in trace + samples)
    assert sum(row.get("wire_rows", 0) for row in trace) == 265
    cw[backend] = dict(trace=trace, timing=samples)

cutoff = json.loads((root / "irn-cutoff-regression/acceptance.json").read_text())
assert len(cutoff) == 18
archive = dict(
    task12=dict(status="accepted", scope="one prescribed path transition, one IRN QP, ordered receiver VOQ", backends=cw),
    task13=dict(status="accepted", scope="graph and Leaf–Spine port mapping up to 64; graph route/queue tests", backends=tcp),
    irn_cutoff_regression=len(cutoff),
)
(root / "acceptance.json").write_text(json.dumps(archive, indent=2) + "\n")

lines = [
    "# TwiNS 第 12、13 项验收（2026-11-27）",
    "",
    "两项均达到本轮清单的验收范围。独立顺序参照在 a87 运行，HIP 在 RX 9060 XT 运行，CUDA 在 RTX 5090 的既有独占队列运行。原始记录和回执集中在本目录，源文件、复现命令与夹具在 `experiments/paper_switches/`。",
    "",
    "## 第 12 项 · ConWeave 接收端重排",
    "",
    "移植的是单个 IRN QP、单次预设路径切换的接收端 VOQ 状态机。两条路径等跳数，交换机间传播分别为 8 µs 和 2 µs。作者顺序参照用预设 TxToR 选择和真实 Qbb 转发去触发作者自己的 RxToR 管线及 VOQ。NS-3 中只增加测试驱动、指定此拓扑的 IRN BDP 和观察点。当前支持范围涵盖旧路径更新时间、TAIL 后 1 ns 重定时、丢尾的定时释放，以及有界存储。完整 ConWeave 的自适应选路、控制反馈和 epoch aging 属于后续范围。",
    "",
    "| 顺序用例 | VOQ 峰值 / 释放数 | 发送记录 | 顺序 FCT | HIP 进程 s | CUDA 进程 s |",
    "| --- | ---: | ---: | ---: | ---: | ---: |",
]
cg = {k: grouping([row for row in d["timing"] if row["mode"] != "capacity"],
                   lambda row: row["mode"].split("-")[0]) for k, d in cw.items()}
for mode, name in (("control", "固定路径"), ("tail", "尾包触发"), ("timeout", "缺尾包超时")):
    row = next(r for r in cw["hip"]["trace"] if r["mode"] == mode)
    fct = row["fct"]["expected"][0][-1]
    lines.append(f"| {name} | {row['high_water']} / {row['flushes']} | {row['wire_rows']} | {fct} ns | "
                 f"{timing(cg['hip'][mode])} | {timing(cg['cuda'][mode])} |")
lines += [
    "",
    "三组对照在每个后端均逐交换机匹配状态转移，并逐端口匹配全部 265 条 14 字段发送记录。首个抢先到达的包是序号 3,000，接收 ToR 从 2,000,003,498 ns 开始缓存。TAIL 在 2,000,009,415 ns 到达，七个缓存包在下一纳秒按原序释放。缺尾时在 2,000,041,415 ns 释放，IRN 正常重传，逐包和 FCT 继续匹配。固定路径从未创建待释放 VOQ。容量限制为 1 时，两后端均报告 6 次存储溢出并返回 3。",
    "",
    "首次差分在旧路径首包更新截止时发现尾时间缓存遗漏；首差和修正后的对照均保留。移植通过原来的有序交换机推进合并 VOQ 定时器，复用设备和 MMU 的正常转发。此机制需要同一流的有序更新，正式证据使用串行交换机路径。C++ 手写实现和接线共新增 223 行，其中设备状态机文件 113 行，计入注释及空行。计数不包含顺序端观察脚本，也未记录可靠的人工工时。既有 834 ns IRN 截止/导出实验在最新 RDMA 代码再通过 18 项。",
    "",
    "## 第 13 项 · 48/64 口 TCP",
    "",
    "4 个 Leaf、16 个 Spine，每个 Leaf 连接 16、32 或 48 台主机和全部 Spine。64 口另测 25/100 Gb/s 和 750.137 ns 传播的异构链路。交换机以一个 64 线程块处理宽端口，在块内同步计数、检查及归并。喷洒继续按同一交换机所有入口的原序消费随机数。",
    "",
    "| 宽度、链路、选路 | 完成 / 输入 | 队列记录 | 活跃高号端口 | 高号最大队列 | 同刻竞争组 | HIP 进程 s | CUDA 进程 s |",
    "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
]
tg = {k: grouping(d["timing"], lambda row: (row["case"], row["spray"])) for k, d in tcp.items()}
for row in tcp["hip"]["trace"]:
    key = row["case"], row["spray"]
    route = "喷洒" if row["spray"] else "按流"
    lines.append(f"| {row['case']}，{route} | {row['complete']} / {row['rows']} | {row['queue_rows']:,} | "
                 f"{row['active_high_ports']} | {row['high_max_depth']} | {row['high_same_time_contention']} | "
                 f"{timing(tg['hip'][key])} | {timing(tg['cuda'][key])} |")
lines += [
    "",
    "三次无追踪进程计时用中位数及最小／最大表示。八组在共同 2 ms 截止下逐流六字段、路由表、逐队列顺序和长度均与顺序 ns-3 相同。每后端 174,941 条入队全匹配，末遍零标志。队列深度 24 的用例有预期的未完成流，表格采用顺序端同一截止及同一身份分母。独立高容量正对照用原 64 口异构流表，各模式完成 336/336；两后端另外匹配 73,576 条入队记录。本机的高容量对照使用正常 twins 启动器，自动选中时间编码与 31 段历史。归档保留较短同构历史触发截断标志时的首差，正式结论用末遍零标志的构建。",
    "",
    "机器可读验收见 `acceptance.json`，独立参照源码及库的散列见 `native-sources.sha256`。各个 `comparison.json` 可追溯到每轮命令、整进程耗时、退出码及轨迹。远程结果文件清单见 `results.sha256`。",
]
(root / "REPORT.md").write_text("\n".join(lines) + "\n")
print("Tasks 12 and 13 accepted")
