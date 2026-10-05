# tools/ — Agentic Workflow 工具集

把「大而模糊的开发目标」拆成可并行的子任务，用「文件所有权矩阵 + 跨域波次」调度多个不同厂商的
AI agent（Claude / Codex / GPT 等）协作，并保证**同一文件同一时刻只有一个 agent 在改**。

全部脚本仅依赖 Python 3 标准库（`argparse` / `pathlib` / `json` / `hashlib` / `os` / `fnmatch` /
`re` / `time` / `datetime`），无需安装任何第三方依赖。

## 协调目录

所有工具共享一个「协调目录」，默认 `<git 仓库根>/.agentic`（用 `git rev-parse --show-toplevel`
推断，失败回退当前工作目录下的 `./.agentic`），可用 `--root` 覆盖：

```
<root>/
  registry.json        # agent 注册表（agent_registry.py）
  locks/<sha1>.json    # 文件锁（file_lock.py）
  events.jsonl         # 全局权威事件流（bus.py）
  inbox/<agent_id>.jsonl  # 定向收件箱（bus.py）
```

约定：所有 `--root`、`--plan`、文件路径都以**仓库相对路径**表示，避免把本机绝对路径写死。

## 脚本

### 1. `check_file_ownership.py` — 文件所有权矩阵重叠校验

解析计划文档（Markdown 表格）里每个子任务行末的「改动文件」列，两两比较是否改同一个文件。
重叠判定覆盖：精确路径相等、目录前缀（以 `/` 结尾）、`*`/`?`/`[` 通配（`fnmatch`）。

退出码：`0` 无冲突；`1` 存在冲突；`2` 用法/数据错误。

```bash
# 校验一个准备同波并行的任务集合；有重叠即退出 1
python3 tools/check_file_ownership.py --plan ./PLAN.md P1-1 P2-1 P3-1 P4-7

# 打印全量矩阵与所有现存重叠对
python3 tools/check_file_ownership.py --plan ./PLAN.md --all

# 只看某个任务会碰哪些文件
python3 tools/check_file_ownership.py --plan ./PLAN.md --explain P4-2

# 自定义列标题 / 任务 ID 正则 / 仓库根
python3 tools/check_file_ownership.py --plan ./PLAN.md --header '改动文件' \
    --id-pattern '^T-\d+$' --repo . T-1 T-2
```

### 2. `agent_registry.py` — 文件总线注册表

记录「谁在干什么、占了哪些文件、是否还活着」。重复 `register` 同一 agent 视为更新；
所有写操作都会让 `registry.json` 的 `seq` 自增。

```bash
python3 tools/agent_registry.py register --agent claude-1 --model claude --role implementer \
    --task P2-1 --files "a.gd,b.gd" --branch codex/godot-integration
python3 tools/agent_registry.py heartbeat --agent claude-1
python3 tools/agent_registry.py who-owns "Godot/presentation/shipyard/"
python3 tools/agent_registry.py stale --ttl 300
python3 tools/agent_registry.py release --agent claude-1
```

退出码：`0` 成功；`2` 数据错误（如 agent 不存在）。

### 3. `file_lock.py` — CAS 文件锁

保证「同一文件同一时刻只有一个 agent 在改」。锁文件名为 `sha1(路径).hexdigest() + ".json"`，
`acquire` 采用「读取—校验—原子替换」的 CAS 语义。TTL 默认 900 秒。

```bash
python3 tools/file_lock.py acquire "Godot/presentation/shipyard/shipyard_view.gd" --agent claude-1 --task P2-1
python3 tools/file_lock.py status  "Godot/presentation/shipyard/shipyard_view.gd"
python3 tools/file_lock.py sweep
python3 tools/file_lock.py release "Godot/presentation/shipyard/shipyard_view.gd" --agent claude-1
```

退出码：`0` 成功（含同一 owner 续租）；`1` 锁被他人持有 / 释放非自己持有的锁；`2` 用法错误。

### 4. `bus.py` — 消息总线

agent 间通讯。所有消息写入 `events.jsonl`；点对点消息额外写入 `inbox/<to>.jsonl`；`--to '*'`
为广播（只写事件流）。合法类型：`CLAIM / RELEASE / CONFLICT / PROPOSE / ACCEPT / REJECT /
REQUEST_HELP / INTERRUPT / HANDOFF / BLOCKED / DONE`。

```bash
python3 tools/bus.py send --from claude-1 --to codex-2 --type CONFLICT --task P2-1 \
    --body "shipyard_view.gd 与你的任务重叠" --refs "P2-1,P3-1"
python3 tools/bus.py read --agent codex-2 --since 0
python3 tools/bus.py tail --n 20
```

退出码：`0` 成功；`2` 非法消息类型。

### 5. `validate_domains.py` — 功能域树校验

校验功能域树 JSON（`templates/` 与 `examples/` 共用同一结构）。规则：`version` 存在；`id` 全局
唯一且非空；`level` 与嵌套深度一致（root=1）；**最大深度 ≤4**；`leaf: true` 的节点须满足停止规则
（`files` 数量 ≤3 且 `acceptance` 非空，无 `files` 字段则跳过文件数检查但仍需 `acceptance`）；
非叶子节点须有非空 `children`；同一父节点下的兄弟叶子不得重复声明同一文件。

```bash
python3 tools/validate_domains.py domains.json
python3 tools/validate_domains.py domains.json --json
```

退出码：`0` 通过；`1` 存在违规（逐条列出）；`2` 文件缺失/JSON 非法。

## 嵌入 agent 工作流（推荐节奏）

```
① 分派前：check_file_ownership.py <同波任务 ID...>   # 退出 0 才允许分派
② 开工：  agent_registry.py register …              # 登记身份/任务/文件/分支
③ 取锁：  file_lock.py acquire <文件> --agent … --task …
          期间：agent_registry.py heartbeat（周期刷新）
                bus.py send --type CLAIM/CONFLICT/REQUEST_HELP/HANDOFF …
④ 收工：  file_lock.py release <文件> --agent …
          agent_registry.py release --agent …      # 或 bus.py send --type DONE
（看板：agent_registry.py list / who-owns、file_lock.py list / sweep、bus.py read / tail）
```

单条可复制的最小闭环示例：

```bash
ROOT=$(mktemp -d)/.agentic
python3 tools/agent_registry.py --root "$ROOT" register --agent a1 --model gpt --role implementer \
    --task T-1 --files "a.gd" --branch main
python3 tools/file_lock.py --root "$ROOT" acquire "a.gd" --agent a1 --task T-1
python3 tools/bus.py --root "$ROOT" send --from a1 --to '*' --type CLAIM --task T-1 --body "抢占 a.gd"
python3 tools/file_lock.py --root "$ROOT" release "a.gd" --agent a1
python3 tools/agent_registry.py --root "$ROOT" release --agent a1
```
