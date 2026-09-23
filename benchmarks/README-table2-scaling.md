# Table 2 峰值内存配置

2026-09-23：Graph500 已按用户要求切换为缓存 CSR 图、32 次 BFS 和真实 OpenMP。
下表的 Graph500 行保留旧协议的历史测量；新协议峰值已测得19.523422GiB（-2.3829%），
默认VM配额更新为22040MiB，32次BFS验证通过；不能沿用旧性能分母。
具体命令见 [README-graph500-cache.md](README-graph500-cache.md)。

`config/chameleon-table2-workloads.json` 和 `config/chameleon-table2-memory.json`
已设为默认工作负载和 VM 配额。逐应用入口无需显式传入配置路径：

```bash
python3 benchmarks/scripts/run-sized-chameleon-apps.py --name default-apps-new
# 回到旧小规模参数和内存表：
python3 benchmarks/scripts/run-sized-chameleon-apps.py --profile small --name small-apps-new
```

`run-chameleon-apps.py` 的工作负载也默认采用 Table 2；它不负责 VM 定容。
原先的 `chameleon-app-memory.json` 由 `--profile small` 使用。显式的
`--workload-config` / `--memory-config` 可以覆盖所选配置，参数匹配检查仍然生效。

论文 Table 2 的 Mem. 一列是 VM 配额。这里按照本次实验要求，把这些数值作为
**应用峰值 RSS 的目标**，允许 **±10%（含边界）**，并为 VM 分配
`ceil_to_2MiB(实测峰值 + 2048 MiB)`。这不是论文原始 VM 配额的复刻。

峰值在关闭 Chameleon 回收、所有页位于本地时测量，取 Guest 进程 VmHWM
与 Guest GNU time 最大 RSS 的较大值；不计 Host 上的请求发生器。
只有正常结束且资源、RDMA 健康检查通过的完整运行才可验收。
仅调整堆或缓存上限不能证明达到目标，最终以实测值为准。
±10% 按字节比较并包含上下边界。超出范围的完整运行记为 `ADJUST_REQUIRED`。
内存计划与工作负载参数绑定，参数改变后必须重新测量；对于已验证的计划，
XSBench/Graph500 使用实测计划替代保守的内存估算启动检查。

## 实测验收（2026-09-22）

8 个可运行应用均完成运行并通过 **±10% 峰值验收**。以下为选定配置的一次完整
Guest 测量；不是多次运行的波动区间保证。每次运行的具体检查范围见汇总中的
`application_check`。Liblinear/PVC 已记录后续重跑参考；GraphChi 尚无独立数值 oracle。

| 应用 | 目标 GiB | 实测峰值 GiB | 偏差 | VM 内存 MiB |
|---|---:|---:|---:|---:|
| graphchi | 23 | 23.966 | +4.20% | 26590 |
| cassandra | 48 | 46.417 | -3.30% | 49580 |
| liblinear | 32 | 31.686 | -0.98% | 34496 |
| xsbench | 50 | 49.974 | -0.05% | 53222 |
| memcached | 32 | 32.073 | +0.23% | 34892 |
| spark-kmeans | 15 | 14.556 | -2.96% | 16954 |
| graph500 | 20 | 19.530 | -2.35% | 22048 |
| pvc | 16 | 15.945 | -0.35% | 18376 |

汇总：[summary.json](results/chameleon/table2-peak-summary/summary.json)。
固定 VM 配额：[chameleon-table2-memory.json](config/chameleon-table2-memory.json)。
所有校准运行关闭 Chameleon 回收；新的大配置尚未执行一整轮 Chameleon 回收率实验。

后续单应用检查：53222 MiB VM 的大配置 XSBench 开启 Chameleon 后，在初始化
阶段被 Guest OOM 杀死。已观察到双向 RDMA，但该应用运行记为 FAIL，原因尚待定位。
小规模 XSBench 完整运行及 checksum 通过，确认真实远端读写。两次运行的证据见
[RDMA 确认报告](results/chameleon/xsbench-rdma-confirm-evidence/report.json)。

## 输入和运行

```bash
cd /path/to/chameleon-ae
python3 benchmarks/scripts/prepare-table2-inputs.py
# 校准：逐应用新建启动周期，连接真实 VF/RDMA，关闭回收以测 RSS。
benchmarks/scripts/run-table2-apps.sh --name table2-profile \
  --reprofile --profile-only --profile-memory-mib 65536
# 汇总完整 all-local 运行；仅生成通过验收的内存计划。
python3 benchmarks/scripts/summarize-table2-peaks.py \
  benchmarks/results/chameleon/table2-profile \
  --output benchmarks/results/chameleon/table2-peak-summary --record-config
# 重新校准后，确认 summary.json 的 status=PASS，再更新固定配置：
cp benchmarks/results/chameleon/table2-peak-summary/memory-config.json \
  benchmarks/config/chameleon-table2-memory.json
# 使用新配置开启 Chameleon：每个应用重启 VM，按其峰值+2GiB 与 vCPU 数分配。
benchmarks/scripts/run-table2-apps.sh --name table2-chameleon
```

64 GiB 校准 Guest 需要相应 VFIO DMA 映射额度与 memlock 上限。大输入和
GraphChi 分片需要足够磁盘；本机 Guest 磁盘已从 20 GiB 扩至 200 GiB。
`run-sized-chameleon-apps.py` 会在退出时恢复进入脚本时的 VM 配置与服务状态。
输入准备脚本持有 VM 控制锁，避免与运行批次和重启并发。

## 选定的工作负载参数

| 应用 | 输入或主要参数 | vCPU |
|---|---|---:|
| GraphChi | Twitter 前 10.5 亿条边；2 shards；内存预算 28000 MiB；20 次迭代 | 4 |
| Cassandra | 400 万条记录；100 万次 Zipf 读取；16 个客户端线程；8 connections；加载 10000 ops/s；堆 45056 MiB | 12 |
| Liblinear | KDD12 前 6800 万行；solver 6；4 线程 | 4 |
| XSBench | gridpoints 102400；1700 万次查询；4 线程；reference checksum 969477 | 4 |
| Memcached | 缓存 32768 MiB；12 服务线程；800 万个真实 SET 键预热；120 s / 0.01 Mops/s replay | 12 |
| Spark-KMeans | 输入前 3000 万行；driver 堆 16384 MiB；16 partitions；8 线程 | 8 |
| Graph500 | SCALE 25；edgefactor 18；8 线程；64 次 BFS 验证 | 8 |
| PVC | 1600 MiB 合成输入；4 线程；1 次执行 | 4 |

实际传入的完整参数以 `config/chameleon-table2-workloads.json` 为准。
这些参数匹配内存目标，不表示输入、吞吐率和论文实验全部相同。

## 配置来源与检查

- Liblinear：真实 KDD12 的行前缀，solver 6；保留预测结果作为后续重跑参考。
- XSBench：H-M large / event / unionized 自定义网格。自定义网格不适用上游
  `952131` 校验值；本配置采用同一网格、相同查询次数的 nuclide-search
  reference。原始退出状态与 checksum 均保留，只有与显式 reference 一致才接受。
- Graph500：调整 SCALE / edgefactor，仍执行并验证全部 64 次 BFS。
- GraphChi：真实 Twitter 边前缀，调整边数、分片及内存预算；保留 PageRank 输出。
  退出成功不是独立的 PageRank 数值正确性证明。
- Spark-KMeans：已有 KMeans 输入文件的行前缀，调整 driver 堆和分区数；验证
  两次同次运行的 cost 扫描一致。初始化未固定随机种子，不跨次比较精确 SSE。
- PVC：现有固定记录格式的确定性生成器；记录输入规模和输出 counts/checksum。
  该输入为合成数据，不声称是论文 Wikipedia English 原始 trace。
- Cassandra：YCSB Zipf 访问，记录条目数和 JVM 堆。RSS 包括 JVM 的预触碰堆，
  不等同于数据库有效数据量；检查所有 load/run 操作结果。
  大配置通过 `--load-target 10000` 限制加载速率；该选项默认仍是 0（不限速）。
  `check-ycsb-result.py` 同时校验完成数量和返回码，加载未完成时不会继续读阶段。
  YCSB 进程退出码 0 本身不能证明所有记录已成功写入。
- Memcached：先用真实 CacheLib trace 中的 distinct SET 键和 4096 字节值预热，
  再重放 trace。键填充规则与原发生器一致，不添加虚构键。
  预热通过 Guest 内 TCP 完成，不添加 Host TCP 转发；正式请求仍使用原 UDP 路径。
  预热包含在运行窗口内，保存缓存 bytes/items/evictions；Host 发生器不计入峰值。
- SPEC 602.gcc_s：缺少有授权的可运行安装，标为 unavailable，不生成虚假的测量。

应用请求的网络路径与 Hermit RDMA 是两回事；关闭回收的校准运行虽然保持
真实 RDMA 连接，但不应据此宣称发生远端换页。

配置里的 `candidate-not-measured` 表示尚未验收。实测后记录具体峰值、相对误差、
证据路径与 `measured-pass` / `adjust-required`，不能把候选参数当成通过结果。
