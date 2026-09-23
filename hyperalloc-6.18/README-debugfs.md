# Chameleon debugfs 运行时参数接口

Guest 和 Host 参数均可通过 `cat` / `echo` 读写，无需为每次参数修改重新编译。参考 Atlas/Hermit 的逐文件接口形式，但写入经过范围验证和现有控制锁，避免工作线程读到半更新配置。原有 `control` 命令、`stats` 和 QMP 接口保留，**内核启动默认值、QEMU 默认值和 benchmark 脚本的既定配置均未改变**。

使用这些接口需要安装包含相应实现的 Guest 内核、Host KVM 和 QEMU。更新 Host KVM 和 QEMU 后，需要重启对应 VM。安装步骤见[环境部署说明](../docs/environment.md)。

## 基本用法

在各自机器的 root shell 中操作，文件权限为 `0600`；只读的 `Lptw` 为 `0400`。如未挂载：

```sh
mount -t debugfs debugfs /sys/kernel/debug
```

Guest 示例：

```sh
tracker=/sys/kernel/debug/chameleon
manager=/sys/kernel/debug/chameleon_mm
policy=/sys/kernel/debug/chameleon_policy

# 固定采样和 cooling：先设置保存值，再切换模式。
echo 8192 > "$tracker/fixed_sample_period"
echo 0 > "$tracker/sampling_adaptive"
echo 131072 > "$tracker/fixed_cooling_samples"
echo 0 > "$tracker/cooling_adaptive"

# PSI some、1%、10 ms；空闲页与冷页回收同时开启。
echo 0 > "$policy/psi_full"
echo 10000 > "$policy/threshold_ppm"
echo 10000 > "$policy/epoch_us"
echo 512 > "$policy/free_pages"
echo 16 > "$policy/cold_folios"

# HHH δ=0.7；维护完成后间隔5秒。
echo 700 > "$manager/hhh_dominance_permille"
echo 5000 > "$manager/maintenance_interval_ms"
echo 0 > "$manager/split_mode"
echo 0 > "$manager/selector"

# 初次配置整组成本参数仍可使用原有原子命令。
echo 'batch 32 6400 128 4 10' > "$manager/control"
for order in 0 2 3 4 5 6 7 8 9; do
    echo "$((1 << order))" > "$manager/Creclaim_order$order"
done
# 后续可单独调整一个参数，例如：
echo 6400 > "$manager/Csync"
cat "$manager/Lptw"

cat "$tracker/stats"
cat "$manager/stats"
cat "$policy/stats"
```

以上设置参数；启动/停止控制器仍使用现有 `echo enable/disable > .../control`。参数不是持久化配置，重启后恢复启动默认值。应用 runner 启动时仍写入既定实验配置；手动修改参数应在 runner 完成初始化后进行，或相应修改实验配置来源，避免下次启动覆盖。

## Guest tracker：`/sys/kernel/debug/chameleon/`

| 文件 | 内核启动默认值 | 有效范围/单位 | 生效规则 |
|---|---:|---|---|
| `sampling_adaptive` | 1 | 0=fixed，1=adaptive | 开启时也可切换，更新已有 PEBS 事件周期 |
| `fixed_sample_period` | 4096 | 512–4294967295 events | fixed 时立即重编程；adaptive 时只保存固定值 |
| `cooling_adaptive` | 1 | 0=fixed，1=adaptive | 立即按选定间隔更新冷却状态 |
| `fixed_cooling_samples` | 启动时 `totalram_pages()` | 1–2^40 samples | fixed 时生效；缩短间隔会处理已经跨过的冷却周期 |
| `hotset_target_percent` | 30 | 1–100，百分比 | 立即根据 histogram 重新计算 φ |

`phi` 本身仍是动态计算结果，在 `stats` 中读取；调节的是热集目标比例。Guest 初始 `totalram_pages()` 不包含全部 VM 配置容量，policy 启用后通过实际容量反馈更新 tracker；不能拿启动时 tracker 的值替代实验的 VM 内存分母。

## Guest manager：`/sys/kernel/debug/chameleon_mm/`

| 文件 | 内核启动默认值 | 有效范围/含义 |
|---|---:|---|
| `hhh_dominance_permille` | 700 | 500–1000，千分比；700 与原 δ=0.7 判断精确等价 |
| `maintenance_interval_ms` | 5000 | 1–3600000 ms；每轮工作结束后延迟；运行时修改会重新安排下一轮 |
| `scan_folios` | 128 | 1–128；维护/aging 的合格候选处理上限 |
| `split_mode` | 0 | 0=HHH，1=Memtis |
| `selector` | 0 | 0=mixed_cost，1=linux_lru |
| `memtis_min_bin` | 20 | 1–20，仅 Memtis 对照模式使用 |
| `memtis_budget` | 32 | 1–128 folio，仅 Memtis 对照模式使用 |
| `batch` | 0，未配置 | 1–128，成本模型的 K，单位为 folio |
| `Csync` | 0 | 0–U64_MAX，显式同步成本 |
| `Etrans` | 0，未配置 | 1–U64_MAX，转换条目数 |
| `Nactive` | 0 | 0–U64_MAX，活动上下文数 |
| `Lptw_fallback` | 0，未配置 | 1–U64_MAX，显式 PTW 延迟/回退值 |
| `ptw_auto` | 1 | 0=手动 fallback，1=PMU 自动更新 |
| `Lptw` | 0 | **只读**，当前实际使用值 |
| `Creclaim_order0`、`Creclaim_order2`…`Creclaim_order9` | 0，未配置 | 0–U64_MAX，每个 order 的回收成本；无 order1 文件 |

成本默认零值表示尚未配置，未把实验脚本的 `32/6400/128/4/10` 移入内核默认值。单参数设置齐 `batch/Etrans/Lptw_fallback` 后，成本模型才有效；每个 order 还需显式配置。`Creclaim_orderN=0` 表示该 order 使用零回收成本，兼容旧 `cost` 命令。计算溢出时该选择项不可用，保留原保护。

`scan_folios` 不扩展静态 128 项候选数组。有目标进程过滤时，为找到合格候选可能遍历更多 folio，因此它不是所有情况下原始遍历次数的硬上限。持有手工隔离候选时修改 `selector` 返回 `EBUSY`，须先 `echo putback > control`。

调试固定 PTW 成本：

```sh
echo 0 > /sys/kernel/debug/chameleon_mm/ptw_auto
echo 10 > /sys/kernel/debug/chameleon_mm/Lptw_fallback
cat /sys/kernel/debug/chameleon_mm/Lptw
# 恢复PMU自动测量：
echo 1 > /sys/kernel/debug/chameleon_mm/ptw_auto
```

写 `Lptw_fallback` 会立即更新实际 Lptw 并清除 measured 标记，与旧 `batch` 命令一致；自动模式下一次有效 PMU 更新仍可覆盖它。切回自动模式时重新建立测量基线。

## Guest policy：`/sys/kernel/debug/chameleon_policy/`

| 文件 | 内核启动默认值 | 有效范围/单位 |
|---|---:|---|
| `epoch_us` | 1000 | 1000–1000000 μs |
| `threshold_ppm` | 10000 | 0–1000000；10000 = 1% |
| `psi_full` | 0 | 0=memory some，1=memory full |
| `free_pages` | 512 | 0–2^32，必须为512的倍数；4 KiB页/轮 |
| `cold_folios` | 8 | 0–128 folio/轮 |
| `minimum_local_bytes` | 0 | 0 或 [2 MiB,2^52] bytes；0在启用时解析为 VM 容量一半 |
| `discard_test` | 0 | 0/1；1需要 CONFIG_CHAMELEON_TEST，仅用于原有无数据后端测试 |

生产参数可在线更新：持锁等当前 worker epoch 完成后修改，下轮使用新值。`epoch_us` 不取消已排定的 timer，下一次 timer 回调重装新周期。在线修改 `minimum_local_bytes` 不能超过实际 VM 总内存；在线写 0 当即解析为总内存一半。提高下限只限制后续回收预算，不会立刻恢复已有远端页面。

运行中不能同时将 `free_pages` 和 `cold_folios` 置零；`discard_test` 必须在控制器关闭时改。失败清理尚持有 lease 时拒绝参数修改。原 `control` 的 `set ...` 仍维持只允许关闭状态下写入的约定，新增逐参数文件才提供在线更新。

`free_pages=0` 关闭主动空闲页回收；512 每轮最多回收2 MiB。它不是百分比，也不是开关值1。此前 cold-only 实验用0，**当前应用实验配置已经是512**。

原实验回收预算 `min(6144 MiB, VM_memory−2048 MiB)` 是脚本计算规则；对应内核参数为本地下限，空闲页与冷页共用预算。例如7700 MiB VM：

```sh
# min(6144,7700-2048)=5652 MiB，因此本地下限为2048 MiB。
echo 2147483648 > /sys/kernel/debug/chameleon_policy/minimum_local_bytes
```

## Host EPT：每 VM 独立目录

目录为 `/sys/kernel/debug/kvm/<QEMU-PID>-<VM-FD>/chameleon/`。先按当前 QEMU PID 找到对应目录，不要写另一台 VM：

```sh
ls -d /sys/kernel/debug/kvm/*/chameleon
# 将下面示例替换为上一条输出中目标QEMU的真实目录。
host_knobs=/sys/kernel/debug/kvm/12345-10/chameleon
cat "$host_knobs/ept_mode"
echo deferred > "$host_knobs/ept_mode"
echo 512 > "$host_knobs/batch_pages"
echo 0 > "$host_knobs/watermark_bytes"
```

| 文件 | 原QEMU默认值 | 有效范围/语义 |
|---|---:|---|
| `ept_mode` | deferred | `deferred`/`immediate`，也接受0/1；读取模式名称 |
| `batch_pages` | 512 | 1–U64_MAX，4 KiB页；触发 READY 范围合批的阈值 |
| `watermark_bytes` | 0 | 0–U64_MAX bytes；0关闭 Host 可用内存低水位触发 |

EPT batch_pages、Guest成本模型K、Guest每轮free_pages是三个不同参数。QEMU启动参数仍可覆盖这里的默认值。

这三个决策原来位于 QEMU，而非 KVM 内核。现在 KVM 保存每 VM 的统一请求配置，QEMU 通过私有 `KVM_CHAMELEON_TUNING` 接口同步；**QMP 和 debugfs 修改同一配置**。QEMU在每次批次决策、QMP查询和现有100 ms timer中同步，实际延迟也包含线程调度时间。已在途的 FINALIZE batch 保留创建时的 EPT 模式，下一批使用新值。原QMP对在途batch改mode的拒绝行为保留。

旧QEMU未注册该接口时，文件读写返回 `ENOTCONN`；新QEMU在旧KVM上仍可使用原QMP方式，并打印兼容回退说明，但无法使用 Host debugfs 参数接口。Host配置读取出错时暂停新批次并记录日志；QMP可能仍显示最后的缓存值，不能据此认定新参数已经生效。普通非法值在修改前拒绝；私有ioctl的copyout故障可能发生在配置已提交之后，此时应重新GET核实。

## 构建和复测

从 `hyperalloc-6.18/` 执行；以下命令在隔离测试 VM 中检查接口行为：

```sh
# 使用现有配置增量构建；首次构建参见主README。
make -C linux O="$PWD/build/guest" CC=clang LOCALVERSION= -j24 bzImage modules
make -C linux O="$PWD/build/host" LOCALVERSION= -j24 bzImage
ninja -C build/qemu qemu-system-x86_64
make -C tests module userspace kvm-chameleon-tuning kvm-chameleon-control
python3 scripts/make-guest-initramfs.py

python3 scripts/test-chameleon.py --stage tuning --name debugfs-guest-rerun
python3 scripts/make-nested-initramfs.py --chameleon-control-only \
  --chameleon-tuning-tests --output build/debugfs-host-control.cpio.gz
python3 scripts/test-chameleon-host.py --tuning \
  --initramfs build/debugfs-host-control.cpio.gz --name debugfs-host-rerun
python3 scripts/make-nested-initramfs.py --chameleon --chameleon-policy \
  --host-shell --output build/debugfs-policy-host.cpio.gz
python3 scripts/test-chameleon-policy.py --tuning --regression \
  --initramfs build/debugfs-policy-host.cpio.gz --name debugfs-policy-rerun
```

`--host-shell` 是隔离测试镜像选项，为runner在L1中写debugfs提供串口shell；普通部署不需要。生产磁盘包使用既有 `kernel-deploy.py --role guest/host build/package` 和 `build-deploy-qemu.sh`，安装流程见[环境部署说明](../docs/environment.md)。Guest只更新内核即可获得Guest文件；Host接口须同时更新KVM和QEMU，并重启对应VM。
