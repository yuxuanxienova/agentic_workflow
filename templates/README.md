# 模板索引（templates/）

本目录是「Agentic Workflow 标准」的可复制模板集。每个文件一句话用途如下：

| 模板 | 一句话用途 |
| --- | --- |
| `domain-tree.schema.json` | 功能域树（≤4 级）的 JSON Schema，含「files ≤3 且 acceptance 非空 或 level=4」停止规则。 |
| `domain-tree.example.json` | 功能域树最小可运行示例（1 项目 + 2 一级域，其一展开到 3 级带叶子）。 |
| `task-breakdown.md` | 任务拆分模板：固定表格 + 子任务粒度三条件 + 五步流水线。 |
| `ownership-matrix.md` | 文件所有权矩阵：按文件分组视图 + 分派前置核查表 + 重叠串行硬规则。 |
| `acceptance.md` | 三级验收模板：L0/L1/L2 最低证据清单与红线。 |
| `handoff.md` | 任务交接模板：起止 SHA、改动面、证据、遗留、所有权释放。 |
| `registry.schema.json` | agent 注册表 schema（角色 / 状态 / 文件 / 心跳）。 |
| `lock.schema.json` | 文件锁 schema（单文件排他锁 + TTL）。 |
| `message.schema.json` | agent 间消息 schema（CLAIM/RELEASE/CONFLICT/HANDOFF 等 11 类型）。 |

## 使用顺序

```
① domain-tree        （先圈功能域，到叶子为止）
        │
        ▼
② task-breakdown     （每域列子任务，填「改动文件」）
        │
        ▼
③ ownership-matrix   （按文件分组 → 查重叠 → 排波次，退出 0 才分派）
        │
        ▼
④ 执行中写 handoff    （每个子任务结束即交接 + 释放文件所有权）
        │
        ▼
⑤ 每步过 acceptance   （L0 → L1 → L2，未过门槛不提交）
```

- ①→②→③ 是**分派前置**，未完成不得并行分派。
- ④ 与 ⑤ 在**执行期**持续进行；`registry/lock/message` 三个 schema 供调度器落地 ①③④ 的状态与通讯。
