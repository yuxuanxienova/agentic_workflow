# Agentic Workflow

一套**独立于项目**的多 Agent 协作标准：把模糊的大目标拆成可并行、可验收、且不会互相踩文件的任务网络。

> 核心命题：**并行度上限 = 独立文件链数。** 想让 N 个 agent 真正并行，必须先把工作切到 N 组互不相交的文件上。

本仓库自身即一个 git 项目。别的项目要用时，`git clone` 到项目下即可（见「快速上手」）。

## 组成

| 目录 | 内容 |
| --- | --- |
| [`agentic_workflow.html`](agentic_workflow.html) | **标准正文**（本文档）：六章 a–f + 图示 + EndlessSpace 实例 |
| [`templates/`](templates/) | 域树 schema、任务表、所有权矩阵、验收 L0/L1/L2、HANDOFF，及 3 个 JSON Schema |
| [`tools/`](tools/) | 6 个可执行 CLI（纯标准库）：重叠校验 / 注册表 / 文件锁 / 消息总线 / 域树校验 / **任务状态跟踪** |
| [`examples/endlessspace-device-parity/`](examples/endlessspace-device-parity/) | 完整运行示例：16 个一级域、48 个子任务、10 条并行链、W1–W8 波次 |

## 六章

- **a 项目解读与功能域树**：树状搜索 → 1–4 级功能域树；叶子停止规则 = ≤3 文件 + 唯一验收。
- **b 任务解读与拆分**：模糊目标 → 影响域集 → 子任务（每任务带「改动文件」列，作为唯一数据源）。
- **c 并行模型**：按文件聚合出串行链 → 跨域波次 + 甘特；波内零文件重叠。
- **d Agent 通讯协议**：文件总线（`.agentic/`）：registry + locks + inbox + events；11 种消息类型。
- **e 岗位与能力矩阵**：Planner / Executor / Reviewer / Integrator（+ Coordinator）。
- **f Git 规范**：混合模型——默认单主干直推，仅「体量大且文件完全独立」才开短命 worktree。

## 快速上手

```bash
# 1. 在目标项目里克隆框架
git clone <本仓库> agentic_workflow

# 2. 校验功能域树
python3 agentic_workflow/tools/validate_domains.py <你的 domains.json>

# 3. 分派前：校验同波任务无文件重叠（有重叠退出 1，必须串行或归同一 agent）
python3 agentic_workflow/tools/check_file_ownership.py --plan PLAN.md P1-1 P2-1 P3-1 P4-7

# 4. 开工加锁 / 收工释放 / agent 间通讯
python3 agentic_workflow/tools/file_lock.py --root .agentic acquire <file> --agent a1 --task P1-1
python3 agentic_workflow/tools/bus.py      --root .agentic send --from a1 --to a2 --type CONFLICT --body "..." 
```

详见 [agentic_workflow.html](agentic_workflow.html) 第 10 章「快速上手」。

## 成熟度

- 域树 / 任务拆分 / 所有权矩阵 / 波次 / 甘特 / Git 规范 / 校验工具：**已在一个真实项目跑通**。
- 文件锁 / 注册表 / 消息总线的**多 agent 真实并发**、协作式中断：**部分验证 / 理论**，尚待跨厂商场景检验。
