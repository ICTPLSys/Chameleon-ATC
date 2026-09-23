# Graph500：缓存图与 32 次 BFS

Graph500 实验配置使用 SCALE=25、edgefactor=18、8 个 OpenMP 线程、32 次 BFS。
第一次单独生成图并保存缓存，正式实验只加载缓存；all-local 和 Chameleon
使用相同的边列表、CSR 邻接结构和 32 个固定起点，每次都验证全部 BFS。

缓存保留验证所需的原始边列表和 CSR 数组，加载到匿名内存中；不以文件映射替代应用内存。
每轮运行仍包含缓存加载和 BFS/验证的完整耗时，生成图、构建 CSR 和写缓存在测量前完成。
正式 workload 配置使用 `--require-cache`，缓存缺失时直接报错。

在 Guest 中先准备缓存（以下示例使用 4 个线程；正式测量使用 8 vCPU）：

```bash
bash ~/chameleon-benchmarks/scripts/run-graph500.sh \
  --scale 25 --edgefactor 18 --threads 4 --bfs-iterations 32 \
  --graph-cache ~/chameleon-inputs/graph500/csr-s25-e18-n32-seed3737844653-v1.bin \
  --prepare-only
```

在 8 vCPU Guest 中加载并运行：

```bash
taskset -c 0-7 bash ~/chameleon-benchmarks/scripts/run-graph500.sh \
  --skip-build --scale 25 --edgefactor 18 --threads 8 --bfs-iterations 32 \
  --graph-cache ~/chameleon-inputs/graph500/csr-s25-e18-n32-seed3737844653-v1.bin \
  --require-cache
```

[`../ae/config/fig78-points.json`](../ae/config/fig78-points.json) 保存了
Graph500 的实验配置。all-local 开启
PEBS（65536 events）和 HHH（15 秒），关闭回收策略。

Graph500 的 all-local、low、medium 和 high 实验点均使用 32768 MiB VM 内存。

构建脚本会应用 [`patches/graph500-csr-cache.patch`](patches/graph500-csr-cache.patch)，
对应上游 commit `6a21c992273f2ba7f742bb64af7bdfe1bc81f101`。
该补丁保存完整 CSR、固定起点并增加 `-n/-L/-W/-P` 参数，同时修正 Makefile 中
命令行 CFLAGS 覆盖 `-fopenmp` 的问题。旧二进制实际只运行一个线程，不能作为新实验分母。
部署脚本会将补丁和 launcher 一起上传；`--skip-build` 要求先构建新版二进制。

缓存文件带版本、字节序、SCALE、edgefactor、seed、BFS 数量和布局信息，
参数不匹配或读取不完整会报错；写入先使用临时文件，完成后再重命名。
改变数据规模、seed 或 BFS 次数时应使用新的缓存路径。
准备日志保存在 `prepare.log` / `prepare-time.txt`，正式运行保存在
`stdout.log` / `time.txt` / `metadata.tsv`。

验证命令：

```bash
python3 benchmarks/scripts/test-graph500-cache.py
```

测试覆盖真实 OpenMP、生成与加载后的 32 次 BFS 验证、固定起点一致、缓存复用，
以及参数不匹配、截断文件和缺失缓存。结果汇总按实际 workload 检查
0–31 的完整验证序列、实际线程数以及 CSR 加载成功，旧历史记录仍按 64 次读取。

## Host 的 numad 与实验绑核

本机 `numad` 曾把新 QEMU 从指定 node 0 调到 node 1，并尝试迁移 VFIO 固定的
Guest 内存，导致绑核预检失败和退出后暂时的 VFIO EBUSY。若 Host 运行 numad，
可在实验前启动仅针对本 VM 的排除守护：

```bash
sudo python3 benchmarks/scripts/guard-qemu-numad.py \
  --run-dir hyperalloc-6.18/build/running/guest-tools-final \
  --stop-file /tmp/chameleon-stop-numad-guard --duration 14400
```

停止文件必须在启动前不存在；实验结束执行 `touch /tmp/chameleon-stop-numad-guard`。
守护核对 QEMU 的用户、程序、VM 名称和 QMP 路径，再调用 `numad -x PID`，
跟随 VM 重启更新排除项，并在停止时撤销。它不停止 numad，也不修改 CPU 掩码；
正式实验仍执行 Host/Guest 绑核验证。启动器对当前启动日志中的 VFIO EBUSY
在原有超时内重试，其他错误仍报错。相关诊断日志保存在本轮结果目录。
