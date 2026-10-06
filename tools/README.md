# tools/ — Agentic Workflow 工具集

把「大而模糊的开发目标」拆成可并行的子任务，用「链（自动）→ 车道（派工）→ 推导波次」调度多个不同厂商的
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
  bus/log.jsonl        # 全局权威消息流（bus.py；全局一份，不分计划）
  bus/inbox/<agent_id>.jsonl  # 定向收件箱（bus.py）
  plans/<name>/        # 每个计划一个文件夹（task_state.py），多个计划可同时 active、互不混写
    plan.json          #   {"name","source","status":"active"|"closed","opened","closed"}
    plan.md            #   计划源文件的副本——工具只解析这份；active 时写事件自动与 source 同步，closed 后冻结
    events.jsonl       #   本计划状态事件（唯一状态真源）
    board.md gantt.md reconcile.md   # 生成物
  index.md             # 所有计划一行汇总（生成物）
```

约定：所有 `--root`、`--plan`、文件路径都以**仓库相对路径**表示，避免把本机绝对路径写死。

**agent ID**（task_state / bus 强制校验）：`<厂商>-<会话/实例>[/<子agent>]`，如 `claude-endlessspace-d5`、
`codex-1`、`claude-endlessspace-d5/e2`。裸厂商名（`claude`）拒绝——日志里分不清是谁，就无法定向协调。

**并发**：task_state 与 bus 的「取 seq + 追加」都在 `flock` 排他锁内完成（`plans/<name>/.events.lock`、`bus/.bus.lock`），
多会话同时写不会撞号。

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

agent 间通讯的**正式渠道**（跨厂商：任何能跑 Python 的 agent 都能读写）。厂商自带的会话消息
（如 Claude Code SendMessage）只用来叫醒对方去读收件箱。所有消息写入 `events.jsonl`；点对点消息额外写入 `inbox/<to>.jsonl`；`--to '*'`
为广播（只写事件流）。合法类型：`CLAIM / RELEASE / CONFLICT / PROPOSE / ACCEPT / REJECT /
REQUEST_HELP / INTERRUPT / HANDOFF / BLOCKED / DONE`。

```bash
python3 tools/bus.py send --from claude-1 --to codex-2 --type CONFLICT --task P2-1 \
    --body "shipyard_view.gd 与你的任务重叠" --refs "P2-1,P3-1"
python3 tools/bus.py read --agent codex-2 --since 0
python3 tools/bus.py read --agent codex-2 --unread   # 只读新消息并推进游标（inbox/<id>.cursor）
python3 tools/bus.py tail --n 20
```

退出码：`0` 成功；`2` 非法消息类型。

### 4b. `bus_wake.py` — 总线唤醒器（Codex）

收件箱有新消息且会话空闲时，经本机共享 app-server daemon 给 Codex 会话注入一轮「去读收件箱」，
ChatGPT App 里实时可见。只对 App「SSH 远程项目（codex-local）」下的会话有效；配置与坑见
`doc/setup_codex.html`。映射/状态/日志落 `bus/wake_*`，本机私有。

```bash
python3 tools/bus_wake.py spawn --agent codex-art-1 --name "负责 plan3 的 art1 车道 · daemon" --text "..."
python3 tools/bus_wake.py map --agent codex-art-1 --thread "<会话名或 UUID>"
python3 tools/bus_wake.py list
python3 tools/bus_wake.py watch [--interval 20] [--direct-only]
```

退出码：`0` 成功；`2` 用法/连接错误；`3` 会话忙已跳过。

#### 省 token：路由 / 条件 / 防抖 / 摘要（`wake_routing.py`）

plan5 实测：Codex 车道 205 次唤醒里 184 次空转，每次空转约 24 万输入 token（重放会话上下文）。
现在由守护进程按规则判断「这条消息跟你有没有关系」，不调模型；按 plan5 历史回放，唤醒 223 → 39 次。

- **路由**：点对点照叫（`ACCEPT` 回执不单独叫；同一发送方/任务的 `CONFLICT` 只叫一次）；广播 `CLAIM/RELEASE/ACCEPT/CONFLICT` 不叫；
  `DONE X` 只叫「后继任务因此真正解锁（前置全 done）」的车道 agent；`BLOCKED/REQUEST_HELP X` 只叫 X 前置的持有者；
  正文点名你一定叫；车道已全部 done 的 agent 只收点对点。协调人照收所有非静默广播。
- **条件唤醒**：等前置的 agent 跑 `bus_wake.py until --agent <我> --task <X[,Y]>`，期间只收点对点 / 点名 / INTERRUPT；
  `task_state.py` 写事件后自动 `check`，条件满足立刻叫醒并清除。`task_state done X` 还会直接叫醒因 X 解锁的后继车道，
  不必等对方广播 DONE。`until --clear` 取消。
- **防抖**：`send` 后先等 `AGENTIC_WAKE_DEBOUNCE`（默认 90）秒，窗口内的后续消息并入同一次唤醒；`INTERRUPT` 不防抖。
- **摘要内联**：唤醒文案直接附上相关消息（点对点全文、广播 300 字预览）和「另有 N 条无关」，并代为推进收件箱游标，
  agent 不用再跑 `bus.py read`。
- 计划解析失败时退回「全部相关」，宁多叫不漏叫。测试：`python3 -m unittest tools/test_wake_routing.py`。

#### 协调人减负与车道规模（`auto_accept.py`、`lanes.py --analyze`、`task_state.py packet`）

plan5 实测协调人一个会话占全部 token 的 36%（文献给的编排开销阈值约 15%）；13 个 agent 实际平均并行度 1.2×。

- **验收 agent（`review_runner.py`）**：`task_state done` → 进 `bus/review_queue.json`；`watch` 每轮看到队列 ≥ 3 条或最早等满
  20 分钟，就后台起 `claude -p --model claude-sonnet-5-5 --effort low --json-schema …`（全新上下文；只读 + git，可在临时
  worktree 跑测试，禁 Edit/Write），材料是每个任务的 `task_state packet` + AGENTS.md 验收章节。结论经
  `task_state review` 写回：pass → ACCEPT 给车道；reject → done 退回 active + REJECT 给车道；unsure → REQUEST_HELP 给协调人。
  失败整批回队列，连续 2 次转协调人。每批耗时 / 费用记 `bus/metrics/reviews.jsonl`。`review_runner.py status | run --force | trial`。
  plan5 真实任务试跑：3 个任务一批约 17 s、$0.22。纯文书问题（计划登记遗漏等）判 pass 并注明，不打回。
- **done 当场机械检查（`auto_accept.py`）**：等级 / 本任务提交 / 改动范围，只提示车道当场补、记进事件 `lint`，不判验收。
- **协调人路由**：DONE 一律不叫醒协调人（验收归验收 agent），只收验收存疑的 REQUEST_HELP。
- **干净上下文材料包**：`task_state.py packet <任务>` 打印规格 + 备注 + 本任务提交 diff（截断），验收 agent 与人工复核共用。
- **1d 换会话**：`token_usage.py` 账本记录每轮上下文大小（`ctx`）；协调人超过 `ROTATE_CTX`（150k）时唤醒文案提示交接换会话，
  报表「当前上下文」列打 ⚠。
- **2a 建议 agent 数 / 2c 关键路径**：`check_file_ownership.py --plan <源文件> --lanes --analyze` 打印硬约束图（同步点 + 文件链）
  每波宽度、建议 agent 数（P75）、尾部串行任务（建议并成收尾车道），以及车道图关键路径每条边的成因
  （同步点 / 共享文件 / 仅车道顺序）和拉长关键路径的文件。不带 `--analyze` 时车道数超标只打一行提示。
- **2b 退场**：`watch` 每轮检查，车道全 done 的 agent 收到一次「车道收工」通知（记 `bus/retired.json`），之后只收点对点。

#### Trae 通道

Trae CN 没有公开 API；`trae_bridge/` 是一个本地扩展，在 `~/.trae-cn/agentic-bridge.sock` 收一行 JSON，
经 Trae 内置 IM 桥命令 `im.bridge.handleIncomingMessage` 把文本投进 **Trae 当前选中的 agent 会话**（聊天面板可见）。
bus_wake 发现桥在线且 Trae 窗口打开本仓库，就把它当作 `trae-1`（可在 `bus/trae_agent.txt` 改）自动叫醒，
`bus.py send --to trae-1` 即触发。Trae 无空闲查询，忙时由 Trae 自己排队，每批新消息只投一次。

```bash
bash trae_bridge/install.sh      # 然后在 Trae 里 Developer: Reload Window
python3 tools/bus_wake.py list   # 出现 trae  trae-1 … 即接通
```

坑：内部命令随 Trae 升级可能改名（3.3.104 实测）；只能投到当前会话；`feishu.bridge.sendAndWaitResponse` 在 SOLO 模式下立即返回空，不要用。

### 4c. `lane_patrol.py` — 车道巡检

补 bus_wake 的盲区：车道卡住却不发消息时没人会被叫醒。launchd `com.endlessspace.lane-patrol` 每 30 分钟跑一次，
检查 无人 / 停滞 / 空转 / 可调序 / 可解锁；**有发现才**以 `claude-endlessspace-patrol` 身份给协调人发 INTERRUPT（会自动叫醒协调人），
同样的发现 2 小时内不重复报。输出 `bus/wake_logs/patrol.out`。

```bash
python3 tools/lane_patrol.py --dry-run          # 只看不发
python3 tools/lane_patrol.py --stale 30 --repeat 120
```

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

### 6. `task_state.py` — 任务状态跟踪（灯化）

解决「一份手改 Markdown 同时当计划+状态+视图」导致的漂移。三层职责分离：

- **PLAN**：计划文档里的任务表——只写计划（任务/改动文件/可选备注），**不写状态**。
- **STATE**：`<root>/plans/<name>/events.jsonl`——append-only，每行=一次状态转移，**唯一状态真源**。
- **VIEWS**：`board` / `gantt` / `reconcile`——全部由本工具生成，**禁止手改**。

状态机：`pending → claimed → active → verify → done`，**允许前跳**（`pending` 直接记 `done` 也行，
只要目标不倒退）；旁支 `非终态 → blocked → active`、`任意非终态 → failed`。
**`done` 没有任何前置条件**——不强制证据、不比较等级、不因缺证据而拒绝；终态（done/failed）只能靠 `revoke` 追加补偿事件回到 active（历史不删）。

> **验收是「灯」，不是「门」。** 想留痕就写 `--note`（自由文本），不写也照样能 done；漏了由 `reconcile` 软报告暴露后重新规划。快速迭代 > 精准验收。

```bash
# 计划生命周期：一个计划一个文件夹
python3 tools/task_state.py plan open plan3 --source Doc/godot/plans/PLAN3.md   # 建 plans/plan3/ 并复制源文件
python3 tools/task_state.py plan sync plan3      # 立即把源文件同步到副本（写事件时也会自动同步）
python3 tools/task_state.py plan list
python3 tools/task_state.py plan close plan2     # 冻结副本与视图，列出未终结任务
python3 tools/task_state.py --plan plan2 migrate P13-2 --to plan3 --agent codex-1   # 旧计划记 migrated，新计划续记
# 写事件（append-only）；--plan 缺省 → $AGENTIC_PLAN → 按任务 ID 在 active 计划里唯一匹配 → 唯一 active 计划
python3 tools/task_state.py claim  P1-1 --agent codex-1
python3 tools/task_state.py start  P1-1 --agent codex-1
python3 tools/task_state.py submit P1-1 --agent codex-1
python3 tools/task_state.py done   P1-1 --agent codex-1 --note "截图 out/...；verify.sh 退 0"
python3 tools/task_state.py block  P4-5 --agent codex-1 --reason "等 P0-12 取证"
python3 tools/task_state.py --plan plan3 revoke P1-1 --agent codex-1 --reason "当时判断有误，重开"
# 视图（缺省 --out 打印到 stdout；写事件后自动写进 plans/<name>/）
python3 tools/task_state.py --plan plan3 list --batch P1
python3 tools/task_state.py --plan plan3 reconcile        # plan 打勾 × 事件日志 × git 工作树 三路对账
python3 tools/task_state.py --plan plan3 seed baseline.json   # 审计收口：一次性灌基线
```

`--note` 是自由文本；兼容旧写法 `--evidence LEVEL:PATH`（折叠成路径串并入备注显示，**不校验**）。

**认领软提示**（灯不是门）：`claim`/`start` 照常成功；若该任务已被别的 agent 持有，或按计划「改动文件」列
与别的 agent 持有中的任务重叠（**含其它 active 计划里的任务**），则打印 `⚠` 并经 `bus.py` 给对方发 `CONFLICT`。持有者 = 最近一次 claim/start
的 agent，done/failed 后清空。对方 ID 不合法（旧日志里的裸 `claude`）时只打印，需人工联系。

**视图自动刷新**：每次写事件成功后，先把 active 计划的源文件同步到 `plans/<name>/plan.md`，再重生成
`plans/<name>/{board,gantt,reconcile}.md` 与 `<root>/index.md`。closed 计划拒绝写事件。

`reconcile` 输出四类差异：计划打勾但非 done / 有事件但未打勾 /
未登记却已改动其文件 / 工作树改动未归属任何任务。**这是体检报告，不是闸门**。

退出码：`0` 成功；`1` reconcile 发现差异（报告信号，非阻塞）；`2` 用法或数据错误（如非法转移）。

## 嵌入 agent 工作流（推荐节奏）

```
① 分派前：check_file_ownership.py <同波任务 ID...>   # 退出 0 才允许分派
② 开工：  agent_registry.py register …              # 登记身份/任务/文件/分支
③ 取锁：  file_lock.py acquire <文件> --agent … --task …
          期间：agent_registry.py heartbeat（周期刷新）
                bus.py send --type CLAIM/CONFLICT/REQUEST_HELP/HANDOFF …
④ 状态：  task_state.py claim/start <任务>           # 状态只写事件日志，不手改计划文档
⑤ 收工：  file_lock.py release <文件> --agent …
          写状态：task_state.py done <任务> --agent … --note "…"（可选留痕，无前置）
          agent_registry.py release --agent …      # 或 bus.py send --type DONE
（看板：task_state.py board/gantt/reconcile、agent_registry.py list / who-owns、
  file_lock.py list / sweep、bus.py read / tail）
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
