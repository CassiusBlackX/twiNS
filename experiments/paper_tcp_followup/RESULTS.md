# 11 月 26 日 TCP 后续验收

Astra 在 `materials/TODOS.md` 新增的四项均已完成。本机 RX 9060 XT 与 s50 RTX 5090 各通过 18 项定向验收、20 项 TCP 选项矩阵、796 项双关发布回归，另各通过 3 项多世界重试检查。所有被接受的运行均匹配对应顺序参照，终态无容量溢出或未决平局。

原始资料保存在本机 `/home/cassius/Work/SpecDES/twins-paper-tcp-options/out/tcp-followup-20261126/`。主要入口为 `status.json`、`hip/release-targeted/acceptance.json`、`cuda/targeted/acceptance.json`、两端的矩阵与回归汇总、`hip/batch/acceptance.json` 和 `cuda/batch/acceptance.json`。失败探针、原有基准和冻结参照均保留。

## RTO：13 次到期，12 次进入丢失状态

顺序驱动原先统计 `CongState` 进入 `CA_LOSS` 的次数；GPU 统计有数据待处理的 RTO handler。`native_rto.py` 只在原生 `TcpSocketBase::ReTxTimeout()` 增加观测，编译独立对象并重链接隔离的 internet 库。原来的 Unison 工作树、对象和库保持原样。

273 流 NewReno 喷洒输入在两端各有 13 次 handler 到期，涉及 12 条连接。`rto_compare.py` 先按输入及绑定顺序把原生源端口映射到连接，再比较连接身份、皮秒时刻、拥塞状态、累计确认及最高发送序号，13/13 完全一致。在 **2,432,593,800 ps**，连接 128（65→120，源端口 49153）已经处于 `CA_LOSS`，因此不再触发一次状态变化。该差额是计数口径造成的。

原生诊断的 3,092,204 条入队记录仍与冻结参照及 GPU 完全一致。最终两后端矩阵均复核同一轨迹。证据：`rto-by-connection.json`、`native/rto-v2/`、`hip/rto/`；原生精确构建在 `native-builds/native-rto/`。

## 密集连接：修复定时器溢出后的错误输入读取

旧实现每主机每槽只能容纳 16 个到期定时器。溢出的定时器在下一槽变为过期事件，`now` 被夹到槽起点后，代码再用夹过的时间判断事件种类，可能误入包接收分支并读取空输入。新诊断保留了 `TCP_OVERDUE` 记录；修复保留原始候选时刻来选择事件种类，并用独立时间执行 handler。

到期 scratch 改为逐主机偏移。默认入口把 `due-ovf` 与 `active-ovf` 一起纳入重试；预分配按每连接至多两类发送端及四类接收端定时器保留空间，并复用已消费或取消的 scratch 项。只有实际无法写入才报溢出。

单源 270 条 NewReno 连接的双关、双开，分别通过自动扩容与预分配，两后端各四组均完成并匹配全部 270 条流的六字段，最终所有标志为零。自动路径的首遍溢出及重跑耗时保留在日志中。证据：两端 `targeted/dense/`（HIP 为 `release-targeted/dense/`）和 `native/dense/`。原始故障仍在上一批 `out/tcp-options/dense-cutoff/`。

## 优化 HIP：127 段历史不再依赖 O0

保留优化构建的首个故障后，把发包临时帧、重组临时帧与当前事件历史移入每主机独占的设备工作区；发包和接收使用不同帧，避免接收 handler 发 ACK 时覆盖输入。结合已有的队列内宽帧处理，去掉 host 到达帧的不必要私有副本。此修改消除了本例的优化宽私有存储故障路径；未把它描述为已确认的上游编译器缺陷。

ROCm clang 22 / gfx1200 的 127 段目标现使用 **`-O2 -fno-inline-functions`**。DCTCP、CUBIC 各自的仅时间戳和双开共四组，每后端匹配 5,120 条六字段流记录及 **1,625,188** 条有序入队记录，皮秒时间一致且所有标志为零。CUDA 同样通过。证据：`release-build/builds.json`、`release-build/source-hashes.json`、`build/scratch-global/build.log`、两后端 `targeted/wide/`。原始优化失败仍在 `wide-first-fault-acceptance.json` 与 `hip/wide/`。

## IPv4 重组：实现到期回调和 ICMP，解除 30 秒驱动限制

实现与原生 IPv4 相同的逐主机重组超时列表及回调：重组完成时删除列表项，但保留已调度回调；旧回调可以按当前列表重新安排下一次到期。回调继承其真正父事件的历史和主机创建序。首片缺尾片到期时发 58 B ICMP time exceeded 帧；只有尾片的残留项清理时不发 ICMP。ICMP 经正常 FIFO、ECMP 和链路序列化，默认 TCP ICMP 通知不改变 socket 状态。

原生探针调用安装版 ns-3 的公开 IPv4 接收路径。GPU 探针使用生产重组 handler、FIFO 和谱系比较器，在重组边界观测交付。两后端各通过六种逐事件对照：末片在期限前 1 ps、同刻且预先创建、同刻且稍后创建、期限后 1 ps、永久缺失，以及原数据报完成后旧回调为另一未完成项重新定时。交付、清理、ICMP 时刻及同刻顺序全部一致，标志为零。证据：`native/fragments-v2/` 与两端 `targeted/fragments/`。

完整 TCP 入口的两组选项配置均额外通过：

* 故意丢弃第一份尾片，顺序端与 GPU 在 **30,001,008,022,080 ps** 产生 ICMP。两档的全部 452/456 条队列记录匹配，含 ICMP 经过的四个队列，六字段 FCT 匹配。
* 40 条连接从第 1 秒启动到第 40 秒，以 72 秒模拟视界运行。以每主机一个回调槽起跑，按 1→2→4→8→16→32 扩容；最终 40/40 流和全部 960 条队列记录匹配，重跑计入完整墙钟。

原生尾片丢失命令使用 `--threads=0 --topo=file --topofile=two-paths.graph --flows=one.csv --tcp=ns3::TcpNewReno --stop=62 --hostq=red --flowEcmp=1 --queue=200 --tcp-sack=0|1 --tcp-timestamps=1 --dropFirstTail=1`，并设置 `TRACE_ALLQ=62000000000000 TRACE_FRAGMENTS=1`。输入是版本内 `experiments/paper_tcp_options/fixtures/` 的文件。40 连接对照由 `long_native.py` 生成。证据：`native/orphan/`、`native/long-connections/`、两端 `targeted/long/`。

## 批处理、来源与归档

最终驱动按历史深度、活跃／到期容量和回调容量的组合，只重跑受影响世界。两后端的回调扩容均呈现 **2,1,1,1,1,1** 个世界，活跃／到期扩容为 **2,1**。四选项合批匹配全部 1,092 行，只重跑两个时间戳世界，过程为 **4,2,2**。这三组另验证了最终驱动源码 `4286fccc2e4340b99aabec42218e4dc6c04a8434c8654fdffce94d0b4994eec7`。

核心内核及完整构建固定于 Git `6184e1e8edfe877a1f6232cec6adf4fbe6780f58`，18 项 HIP 构建的前后源码哈希一致。最终提交另包含上述只重跑受影响世界的 Python 驱动修正及三项双后端验证；内核与 6184e1e 相同。未重测或替换论文已有性能数据，当前补测用于正确性与支持范围。

CPU 编译、顺序参照与 LaTeX 编译在 a87 `/home/cassius/netsim/twins-tcp-followup-20261126/` 完成；GPU 实验在本机 user systemd 服务中单实例运行，RAM 上限 28 GB。CUDA 遵守旧队列完成门禁及原有独占锁，未在 s50 启动新的 CPU oracle。

CUDA 的 **4,186 个结果文件**和 **18 个可执行文件**均回传本机并按 SHA-256 校验。随后清理了 s50 本批专用临时目录。校验清单为 `cuda-return.sha256`，回执为 `s50-return-receipt.json`。重要数据与二进制均保存在本机。

论文 §4.1、§4.3 与附录 B.2 已据实更新。a87 编译三轮后渲染全部 18 页，并检查改动页；正文仍为 12 页，全文 18 页，无新增溢出或未定义引用。正式 PDF 为 `materials/latex/build/main.pdf`，验收副本与日志在 `paper-check/`。
