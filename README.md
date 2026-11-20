# ebpf-map-eviction-planner

面向「eBPF map 接近容量上限」场景的纯 Python 淘汰规划器：根据每个 key
的最后命中时间、过期时间、引用计数、pin 状态、per-CPU 命中分布和 map
容量，输出**插入 / 更新 / 淘汰 / 拒绝**决策，并给出热点保护证据。

## 不加载真实 eBPF 程序

本项目**完全在用户态用 Python 对象模拟 map 行为**：

- 不调用 `bpf(BPF_MAP_*)` 等任何 syscall，不依赖 `bcc`/`libbpf`；
- 不需要 `CAP_BPF`/`CAP_SYS_ADMIN` 或 root 权限，普通用户即可运行；
- `SimulatedMap` 用字典模拟 map 与 per-CPU 副本，故障通过
  `fault_on_cpu` 注入，用于验证更新冲突和回滚。

它是控制面决策逻辑的原型/规划层，接入真实程序时只需把 `SimulatedMap`
替换为实际的 map 读写适配层。

## 目录结构

```
ebpf_eviction/
  model.py          # MapEntry：key/最后命中/过期/引用计数/pin/CPU 分布
  simulated_map.py  # map 与 per-CPU 副本模拟、容量限制、故障注入与回滚
  planner.py        # 评分、热点保护、过期清扫、插入/更新/淘汰/拒绝决策
tests/
  test_eviction.py  # 过期、冷热竞争、引用阻止、更新冲突四类场景
demo.py             # 端到端演示
```

## 淘汰策略

纯 LRU 会误杀短暂空闲的长连接，纯 LFU 会让新流量永远插不进来，因此
规划器把「年龄」和「热度」合并为一个评分（`planner.py` 的
`eviction_score`，分越高越适合淘汰）：

- 空闲越久、命中越少 → 分越高，优先回收；
- 命中数达到 `hot_hit_threshold` 的热点条目受保护，不参与淘汰；
- `ref_count > 0`（程序正在引用）或 `pinned` 的条目一律不淘汰；
- 过期条目先于容量淘汰被清扫；**已过期但仍被引用的条目保留**，
  等引用释放后的下一轮再回收；
- 若 map 已满且所有存活条目都受保护，新 key 得到 `reject` 决策，
  而不是强行破坏连接状态；
- 每条被跳过的候选都会生成 `ProtectionEvidence`（`referenced` /
  `pinned` / `hot`），作为热点保护的可审计证据。

## per-CPU 与原子更新

- 每个 key 在每个 CPU 上各有一份副本，写入必须覆盖全部 CPU；
- 任一 CPU 写入失败时，已写入的副本恢复旧值，保证所有副本一致；
- 规划器侧的权威状态同步回滚到更新前快照，决策记录标记
  `rolled_back=True`；
- `SimulatedMap.consistent()` 可随时校验每个 key 的 CPU 副本是否齐全。

## 运行

代码按 Python 3.8.2 语法编写（仅使用 3.8 可用的标准库特性）：

```bash
python -m unittest          # 运行全部测试
python demo.py              # 查看每轮变更和保护证据
```

## 输入字段

通过 `EvictionPlanner.build_entry(...)` 构造条目，输入参数对应需求：

| 参数 | 含义 |
| --- | --- |
| `key` | map 查找键 |
| `last_hit` | 最后命中时间戳 |
| `expiry` | 过期时间戳 |
| `ref_count` | 程序引用计数，大于 0 不可淘汰 |
| `pinned` | 是否 pin，pin 后不可淘汰 |
| `cpu_distribution` | 各 CPU 命中分布（per-CPU 镜像） |
| map 容量 | `SimulatedMap(capacity=...)` |
