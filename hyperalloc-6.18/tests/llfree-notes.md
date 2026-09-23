# LLFree 核心移植与验收

基线为 HyperAlloc AE 的 `9f10f948556c530b7b8a8a3cab605591c8d51110`，共同核心提交为 `893d745e7650659274c04a19743bc6d7a189be05`。Guest 和 QEMU 必须同时使用这个提交。原 `child_t`、`tree_t` 和共享 metadata 的 ABI 未改动。

## 修复

1. order 10 分配覆盖两个 2 MiB child。原 `get` 和 `get_at` 仅返回第一个 child 的 reclaimed 位，`E=(0,1)` 会漏掉恢复。现在结果为两位的 OR；调用方仍需检查并恢复范围内的每一个 child。
2. `sync_with_global` 从全局树取走计数后，若 local CAS 失败，必须原数归还。原回滚将 `old_tree` 同时用作 `atom_update` 的输出变量和 `old_tree.free` 参数；宏会先覆盖变量，使回滚错误地使用当前全局计数。现先保存 `stolen_free`。此问题在独立 guest/host local、共同 trees/lower 的并发测试中实际重现，导致最终 free 计数错误，另一次 sanitizer 构建中触发 `tree_inc` 上限断言。
3. 原测试 `alloc_frames()` 未初始化 `ret->sp`，优化构建访问越界崩溃；已初始化为 0。

修复前失败记录分别保留于 `results/llfree-order10-before-fix.log` 和 `results/llfree-counter-before-fix.log`，用于证明新增测试能捕获问题。

## 验收

运行 `./tests/llfree-tests.sh`：

| 构建 | 执行范围 | 结果 |
|---|---|---|
| 原始 AE 基线 | 原有 37 测试 | 通过，历史记录 `llfree-baseline.log` |
| 修复后默认配置 | 全部 41 测试 | 通过 |
| `-O3` 优化配置 | 全部 41 测试 | 通过 |
| `LLFREE_PREFER_INSTALLED=true` | 全部 41 测试 | 通过；与 guest 的偏好配置一致 |
| AddressSanitizer + UndefinedBehaviorSanitizer | 新增 4 项 HyperAlloc 测试 | 通过 |

新测试包含：

- order 10 的两种分配 API × 四种 child E 组合，恢复之后保持分配计数正确且邻接 child 不变。
- order 0–10 的 soft → allocation → install → free；验证 soft 不减少 guest 可分配容量，install 不会释放已分配页面。
- installed/soft → hard → return → allocation/install；验证 hard 页面不能分配，return 保持 E 位，重复 return 被拒绝。
- 4 个 guest worker 各做 3000 次混合 order 0–10 分配/释放，与 host 的 12000 次 soft/hard reclaim 尝试并行。双方独立 local，共享 trees/lower；host 操作由一个 zone mutex 串行化，guest 使用前先完成 install。逐 base-page 检查所有权不重叠，并验证最终所有页面均归还、上下层计数一致。

机器可读结果见 `results/llfree-summary.json`。这些测试验证分配器和元数据协议；Linux 启动、实际 mTHP folio、QEMU backing、EPT 与 VFIO 的验证由集成测试负责。
