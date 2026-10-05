#!/usr/bin/env python3
"""任务状态跟踪（通用 Agentic Workflow 版）。

真源纪律（三层，缺一即乱）：
  PLAN   计划文档里的任务表——只写「计划」（任务/改动文件），**不写状态**。
  STATE  <root>/tasks/events.jsonl——append-only，每行=一次状态转移，**唯一状态真源**。
  VIEWS  board / gantt / reconcile——全部由本工具生成，**禁止手改**。

状态机（允许前跳以降低摩擦；终态只能靠 revoke 反悔）：
  pending → claimed → active → verify → done   （可跳级：pending 直接记 done 也允许）
  旁支：非终态 → blocked → active（unblock）；任意非终态 → failed
  done / failed 是终态；发现判断有误用 `revoke` 追加补偿事件回到 active（历史不删）。

验收是「灯」不是「门」（灰度、快迭代优先）：
  done **没有任何前置条件**——不强制证据、不比较等级。想留痕就写 `--note`（自由文本，
  如截图/日志路径），不写也照样能 done。遗漏由 `reconcile` 软报告暴露，再规划补做即可。

子命令：
  claim / start / submit / done / unblock / revoke    写事件（append-only）
  block / fail                                        旁支（--reason）
  seed <baseline.json>                    一次性灌基线（审计收口用）
  status <task> / list                    查状态
  board / gantt / reconcile               生成视图（board=看板，gantt=车道×波次，reconcile=对账）
  log [--task T] [--n N]                  看事件流

身份（不可区分即不可协调）：--agent 必须是 `<厂商>-<会话/实例>`，如 `claude-endlessspace-d5`、
`codex-1`；子 agent 追加 `/<名>`，如 `claude-endlessspace-d5/e2`。裸厂商名（claude/codex/trae…）拒绝。

认领软提示（灯不是门）：claim/start 照常成功，但若①该任务已被别的 agent 持有，或②按计划「改动文件」
列与别的 agent 持有中的任务重叠，或③任务所在车道归别的 agent，则打印 ⚠ 并经 bus.py 向对方发 CONFLICT；
④前置任务（车道内上一个 + 同步点）未 done 时只打印 ⚠。

链 / 车道 / 波次（见 lanes.py）：链是工具算的约束，车道是唯一派工单位，波次由车道顺序 + 同步点推导。
计划有车道表时 gantt 输出「车道 × 推导波次」；没有时回退旧的手写波次行（§4.3 格式）。

视图自动刷新：每次写事件后自动重生成 <root>/tasks/{board,gantt,reconcile}.md。计划路径取 --plan，
缺省读 <root>/tasks/config.json 的 "plan"（仓库相对路径）；都没有则跳过刷新并提示。

默认 <root> = <git 仓库根>/.agentic。

退出码：0 成功；1 reconcile 发现差异（报告信号，非阻塞）；2 用法或数据错误（如非法转移）。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import fnmatch
import io
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

TASKS_DIR = "tasks"
EVENTS_NAME = "events.jsonl"
CONFIG_NAME = "config.json"
LOCK_NAME = ".events.lock"
VIEW_NAMES = {"board": "board.md", "gantt": "gantt.md", "reconcile": "reconcile.md"}

# <厂商>-<会话/实例>[/<子 agent>]；厂商为小写字母，实例段至少一个字符。
AGENT_ID_RE = re.compile(r"^[a-z]+-[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$")
DEFAULT_ROOT = ".agentic"

STATE_ORDER = ("pending", "claimed", "active", "verify", "done")
BRANCH_STATES = ("blocked", "failed")
ALL_STATES = STATE_ORDER + BRANCH_STATES

# 线性命令 → 目标状态。允许「前跳」：只要目标在状态序上不倒退即可（pending 直接 done 也行）。
LINEAR_TARGETS = {
    "claim": "claimed",
    "start": "active",
    "submit": "verify",
    "done": "done",
    "unblock": "active",
}
RANK = {state: i for i, state in enumerate(STATE_ORDER)}   # pending=0 … done=4
TERMINAL = {"done", "failed"}
BLOCKABLE = {"pending", "claimed", "active", "verify", "blocked"}
FAILABLE = {"pending", "claimed", "active", "verify", "blocked"}

TASK_ID_RE = re.compile(r"^[A-Z]+\d+-\d+$")
OWNERSHIP_HEADER = "改动文件（仓库相对路径）"

MARKERS = {
    "pending": "·",
    "claimed": "○",
    "active": "▶",
    "verify": "●",
    "done": "✓",
    "blocked": "✖",
    "failed": "✗",
}

WAVE_LINE_RE = re.compile(r"^\s*-\s*\*\*W(\d)")
# 链标签紧跟一个 ASCII 冒号，如 `T:` / `S:` / `S′:` / `E1:` / `B10:` / `C:`
CHAIN_LABEL_RE = re.compile(r"([A-Za-z][A-Za-z0-9_\u2032\u2019']*)\s*:")
TASK_TOKEN_RE = re.compile(r"`(P\d+-\d+)`")

# ---------------------------------------------------------------- 基础工具


def default_root() -> Path:
    """默认协调目录：<git 仓库根>/.agentic；推断失败回退 <cwd>/.agentic。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()) / DEFAULT_ROOT
    except OSError:
        pass
    return Path.cwd() / DEFAULT_ROOT


def infer_repo(start: Path) -> Path:
    """从 start 向上逐级查找含 .git 的目录作为仓库根；找不到回退 start。"""
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return current


def now_iso() -> str:
    """当前时间的 ISO 8601（UTC，秒级）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def iter_jsonl(path: Path):
    """逐行解析 JSONL；跳过空行与损坏行。"""
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def append_jsonl(path: Path, payload: dict) -> None:
    """单次写追加一行 JSON（O_APPEND，保持 append-only 原子追加语义）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def events_path(root: Path) -> Path:
    return root / TASKS_DIR / EVENTS_NAME


def load_events(root: Path) -> list[dict]:
    return list(iter_jsonl(events_path(root)))


def next_seq(root: Path) -> int:
    """扫描事件流取最大 seq，返回下一个可用序号。"""
    last = 0
    for entry in load_events(root):
        seq = entry.get("seq")
        if isinstance(seq, int) and seq > last:
            last = seq
    return last + 1


# ---------------------------------------------------------------- 计划文档解析


def split_cells(line: str) -> list[str]:
    """把 `| a | b |` 拆成 ['a','b']（去掉首尾空单元）。"""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def is_separator_row(cells: list[str]) -> bool:
    """判断 Markdown 表格分隔行（如 `| --- | :--- |`）。"""
    return bool(cells) and all(cell and set(cell) <= set("-: ") for cell in cells)


def parse_file_cell(cell: str) -> list[str]:
    """按中/英文分号拆分「改动文件」单元格，去掉反引号与 ./ 前缀。"""
    tokens: list[str] = []
    for part in re.split(r"[；;]", cell):
        token = part.strip().strip("`").strip()
        if token.startswith("./"):
            token = token[2:]
        if token:
            tokens.append(token)
    return tokens


def parse_plan(plan_path: Path) -> dict[str, dict]:
    """解析计划文档任务表 → {任务ID: {batch, title, files, checked}}。

    表头以「分隔行之前那一行」为准，按列标题定位「改动文件」列；
    首单元格去掉 ✅ 后匹配任务 ID 的行视为任务行。
    （不再解析「验收」列——验收已降级为可选自由备注，工具不据此设卡。）
    """
    text = plan_path.read_text(encoding="utf-8").lstrip("\ufeff")
    tasks: dict[str, dict] = {}
    headers: list[str] | None = None
    pending_header: list[str] | None = None

    for line in text.splitlines():
        if not line.strip().startswith("|"):
            headers = None
            pending_header = None
            continue
        cells = split_cells(line)
        if is_separator_row(cells):
            if pending_header is not None:
                headers = pending_header
            continue
        if headers is None:
            pending_header = cells
            continue

        raw_id = cells[0]
        # ✅ 可能写在 ID 单元（`| P0-1 ✅ |`）或子任务单元开头（`| P1-7 | ✅ … |`）
        checked = "✅" in raw_id or (len(cells) > 1 and cells[1].lstrip().startswith("✅"))
        task_id = raw_id.replace("✅", "").strip()
        if not TASK_ID_RE.match(task_id):
            continue

        files: list[str] = []
        if OWNERSHIP_HEADER in headers:
            idx = headers.index(OWNERSHIP_HEADER)
            if idx < len(cells):
                files = parse_file_cell(cells[idx])

        title = cells[1] if len(cells) > 1 else ""

        tasks[task_id] = {
            "batch": task_id.split("-")[0],
            "title": title,
            "files": files,
            "checked": checked,
        }
    return tasks


def parse_waves(plan_path: Path) -> dict[str, dict[str, list[str]]]:
    """解析 §4.3 波次清单 → {波次(如 W1): {链名: [任务ID, ...]}}。

    行形如：`- **W1（峰值 17 并发）**：T:`P1-1` ｜ S:`P2-1` ｜ ...`
    """
    text = plan_path.read_text(encoding="utf-8").lstrip("\ufeff")
    waves: dict[str, dict[str, list[str]]] = {}
    for line in text.splitlines():
        match = WAVE_LINE_RE.match(line)
        if not match:
            continue
        wave = f"W{match.group(1)}"
        chains: dict[str, list[str]] = {}
        for segment in line.split("｜"):
            ids = TASK_TOKEN_RE.findall(segment)
            if not ids:
                continue
            label = CHAIN_LABEL_RE.search(segment)
            chain = label.group(1) if label else "?"
            chains.setdefault(chain, []).extend(ids)
        if chains:
            waves[wave] = chains
    return waves


# ---------------------------------------------------------------- 状态归约


def reduce_states(events: list[dict]) -> tuple[dict[str, str], dict[str, list[str]], dict[str, str]]:
    """从事件流归约出 {任务: 当前状态}、{任务: [备注]}、{任务: 最后更新时间}。

    备注为自由文本；兼容旧事件里的 evidence 数组（折成路径串并入备注显示）。
    """
    states: dict[str, str] = {}
    notes: dict[str, list[str]] = {}
    updated: dict[str, str] = {}
    for entry in events:
        task = entry.get("task")
        if not task:
            continue
        to_state = entry.get("to")
        if to_state in ALL_STATES:
            states[task] = to_state
        legacy = "；".join(
            str(item.get("path"))
            for item in entry.get("evidence") or []
            if isinstance(item, dict) and item.get("path")
        )
        text = "  ".join(x for x in ((entry.get("note") or "").strip(), legacy) if x)
        if text:
            notes.setdefault(task, []).append(text)
        if entry.get("ts"):
            updated[task] = entry["ts"]
    return states, notes, updated


@contextlib.contextmanager
def events_lock(root: Path):
    """多会话并发写：取 seq 与追加必须在同一把排他锁内，否则会撞号。"""
    lock_path = root / TASKS_DIR / LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def append_event(root: Path, **fields) -> dict:
    """按统一 schema 追加一条事件并返回它（持锁取号 + 追加）。"""
    with events_lock(root):
        entry = {
            "seq": next_seq(root),
            "ts": now_iso(),
            "task": fields.get("task"),
            "from": fields.get("from"),
            "to": fields.get("to"),
            "agent": fields.get("agent"),
            "note": fields.get("note") or "",
        }
        if fields.get("claim"):
            entry["claim"] = True
        append_jsonl(events_path(root), entry)
    return entry


def validate_agent_id(agent: str) -> str | None:
    """返回错误信息；合法返回 None。"""
    if AGENT_ID_RE.match(agent or ""):
        return None
    return (
        f"非法 agent ID「{agent}」：须为 <厂商>-<会话/实例>[/<子agent>]，"
        "如 claude-endlessspace-d5、codex-1、claude-endlessspace-d5/e2；裸厂商名不可区分会话，拒绝"
    )


def resolve_plan(args) -> Path | None:
    """--plan 优先；否则读 <root>/tasks/config.json 的 plan（相对仓库根）。"""
    if args.plan is not None:
        return args.plan
    config = args.root / TASKS_DIR / CONFIG_NAME
    if not config.is_file():
        return None
    try:
        plan = json.loads(config.read_text(encoding="utf-8")).get("plan")
    except json.JSONDecodeError:
        return None
    return (args.repo / plan) if plan else None


def refresh_views(args) -> None:
    """写事件后重生成 board/gantt/reconcile，视图永不过期。失败只提示，不影响写入结果。"""
    plan = resolve_plan(args)
    if plan is None or not plan.is_file():
        print(f"ℹ 未找到计划（--plan 或 {args.root / TASKS_DIR / CONFIG_NAME}），跳过视图刷新", file=sys.stderr)
        return
    view_args = argparse.Namespace(**vars(args))
    view_args.plan = plan
    view_args.no_git = False
    renderers = {"board": cmd_board, "gantt": cmd_gantt, "reconcile": cmd_reconcile}
    for name, render in renderers.items():
        view_args.out = str(args.root / TASKS_DIR / VIEW_NAMES[name])
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                render(view_args)
        except Exception as error:  # 视图是派生物，坏了不该连累真源写入
            print(f"ℹ 视图 {name} 刷新失败：{error}", file=sys.stderr)


def linear_allowed(current: str, target: str) -> bool:
    """线性命令是否允许：终态不可离开；blocked 可任意前进；否则目标不得倒退。"""
    if current in TERMINAL:
        return False
    if current == "blocked":
        return True
    return RANK[target] >= RANK.get(current, 0)


# ---------------------------------------------------------------- 写事件命令


def note_text(args) -> str:
    """备注文本：--note 与兼容参数 --evidence（折入）合并，均为自由文本、不校验。"""
    legacy = getattr(args, "evidence", None) or []
    return "  ".join(x for x in ((getattr(args, "note", "") or "").strip(),
                                 "；".join(legacy)) if x)


HOLDING_STATES = {"claimed", "active", "verify", "blocked"}


def current_holders(events: list[dict]) -> dict[str, str]:
    """{任务: 当前持有者}。最近一次认领/开工（→claimed/active）的 agent 成为持有者；终态清空。"""
    holders: dict[str, str] = {}
    for entry in events:
        task, to_state, agent = entry.get("task"), entry.get("to"), entry.get("agent")
        if not task:
            continue
        if to_state in TERMINAL or to_state == "pending":
            holders.pop(task, None)
        elif (to_state in ("claimed", "active") or entry.get("claim")) and agent:
            holders[task] = agent
    return holders


def claim_warnings(args, task: str, states: dict[str, str], holders: dict[str, str]) -> list[tuple[str, str]]:
    """认领时的软提示：[(对方 agent, 提示文本)]。只提醒，不拦截。"""
    warnings: list[tuple[str, str]] = []
    holder = holders.get(task)
    if holder and holder != args.agent and states.get(task) in HOLDING_STATES:
        warnings.append((holder, f"{task} 目前由 {holder} 持有（{states[task]}），{args.agent} 也认领了它；请发 HANDOFF/RELEASE 对齐归属"))
    plan_path = resolve_plan(args)
    if plan_path is None or not plan_path.is_file():
        return warnings
    plan = parse_plan(plan_path)
    lanes_mod = _lanes_module()
    lanes = lanes_mod.parse_lanes(plan_path)
    if lanes:
        lane = lanes_mod.lane_of(lanes).get(task)
        owner = lanes.get(lane, {}).get("agent") if lane else ""
        if lane is None:
            print(f"⚠ {task} 不在任何车道里，先补车道表再派工", file=sys.stderr)
        elif owner and args.agent != owner and not args.agent.startswith(owner + "/"):
            warnings.append((owner, f"{task} 属于车道 {lane}（归 {owner}），{args.agent} 认领了它；跨车道接手须先 HANDOFF"))
        preds = lanes_mod.predecessors(lanes, lanes_mod.parse_sync(plan_path)).get(task, set())
        pending = sorted(p for p in preds if states.get(p) != "done")
        if pending:
            print(f"⚠ {task} 的前置未 done：{', '.join(pending)}（车道顺序/同步点）", file=sys.stderr)
    mine = plan.get(task, {}).get("files") or []
    if not mine:
        return warnings
    shared_files = _ownership_module().shared_files
    for other, other_agent in sorted(holders.items()):
        if other == task or other_agent == args.agent or states.get(other) not in HOLDING_STATES:
            continue
        hits = shared_files(mine, plan.get(other, {}).get("files") or [])
        if hits:
            warnings.append((other_agent, f"{task} 与 {other}（{other_agent} 持有）改动文件重叠：{', '.join(hits)}；请串行或归同一 agent"))
    return warnings


def _lanes_module():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import lanes
    return lanes


def _ownership_module():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import check_file_ownership
    return check_file_ownership


def notify_conflicts(args, task: str, warnings: list[tuple[str, str]]) -> None:
    """打印警告并经总线给对方发 CONFLICT；对方 ID 不合法（如旧日志里的裸 claude）则只打印。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import bus
    for other_agent, text in warnings:
        print(f"⚠ {text}", file=sys.stderr)
        if bus.bad_agent_id(other_agent):
            print(f"  （{other_agent} 不是合法 agent ID，未投递总线，请人工联系）", file=sys.stderr)
            continue
        message = argparse.Namespace(
            root=args.root, sender=args.agent, recipient=other_agent, type="CONFLICT",
            task=task, body=text, refs=task,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_send(message)
        print(f"  → 已向 {other_agent} 发送 CONFLICT（bus.py read --agent {other_agent} --unread）", file=sys.stderr)


def cmd_transition(args, kind: str) -> int:
    """claim/start/submit/done 的通用转移执行（允许前跳，无验收门）。"""
    root: Path = args.root
    events = load_events(root)
    states, _, _ = reduce_states(events)
    current = states.get(args.task, "pending")
    target = LINEAR_TARGETS[kind]
    takeover = kind == "claim" and current not in TERMINAL and RANK.get(current, 0) > RANK["claimed"]
    if takeover:
        target = current  # 接管已推进到 active/verify 的任务：只换持有者，不倒退状态
    elif not linear_allowed(current, target):
        print(
            f"✗ {args.task} 当前为 {current}，不能 {kind} → {target}"
            "（终态不可离开，如需反悔用 revoke；线性转移不可倒退）",
            file=sys.stderr,
        )
        return 2

    entry = append_event(
        root,
        task=args.task,
        **{"from": current, "to": target},
        agent=args.agent,
        note=note_text(args),
        claim=True if kind == "claim" else None,
    )
    print(f"✓ {args.task}: {current} → {target}{'（接管）' if takeover else ''}  #{entry['seq']}  agent={args.agent}")
    if kind in ("claim", "start"):
        warnings = claim_warnings(args, args.task, states, current_holders(events))
        if warnings:
            notify_conflicts(args, args.task, warnings)
    return 0


def cmd_block(args) -> int:
    """block：任意非终态 → blocked。"""
    states, _, _ = reduce_states(load_events(args.root))
    current = states.get(args.task, "pending")
    if current not in BLOCKABLE:
        print(f"✗ {args.task} 当前为 {current}，不可 block", file=sys.stderr)
        return 2
    entry = append_event(
        args.root, task=args.task, **{"from": current, "to": "blocked"},
        agent=args.agent, note=args.reason,
    )
    print(f"✓ {args.task}: {current} → blocked  #{entry['seq']}  原因：{args.reason}")
    return 0


def cmd_fail(args) -> int:
    """fail：任意非终态 → failed。"""
    states, _, _ = reduce_states(load_events(args.root))
    current = states.get(args.task, "pending")
    if current not in FAILABLE:
        print(f"✗ {args.task} 当前为 {current}，不可 fail", file=sys.stderr)
        return 2
    entry = append_event(
        args.root, task=args.task, **{"from": current, "to": "failed"},
        agent=args.agent, note=args.reason,
    )
    print(f"✓ {args.task}: {current} → failed  #{entry['seq']}  原因：{args.reason}")
    return 0


def cmd_revoke(args) -> int:
    """revoke：终态（done/failed）→ active，追加补偿事件（历史不删，供纠错/反证）。"""
    states, _, _ = reduce_states(load_events(args.root))
    current = states.get(args.task, "pending")
    if current not in TERMINAL:
        print(f"✗ {args.task} 当前为 {current}，只有 done/failed 才能 revoke", file=sys.stderr)
        return 2
    entry = append_event(
        args.root, task=args.task, **{"from": current, "to": "active"},
        agent=args.agent, note=args.reason,
    )
    print(f"✓ {args.task}: {current} → active（撤销）  #{entry['seq']}  原因：{args.reason}")
    return 0


def cmd_seed(args) -> int:
    """seed：从基线 JSON 一次性灌入（审计收口用）。"""
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    tasks = payload.get("tasks", payload)
    count = 0
    for task, spec in tasks.items():
        state = spec.get("state", "pending")
        if state not in ALL_STATES:
            print(f"✗ {task} 非法状态 {state}", file=sys.stderr)
            return 2
        note = spec.get("note") or "baseline"
        legacy = "；".join(
            str(item.get("path"))
            for item in spec.get("evidence") or []
            if isinstance(item, dict) and item.get("path")
        )
        if legacy:
            note = f"{note}  {legacy}"
        append_event(
            args.root, task=task, **{"from": None, "to": state},
            agent=spec.get("agent", "audit"), note=note,
        )
        count += 1
    print(f"✓ 已灌入 {count} 条基线事件 → {events_path(args.root)}")
    return 0


# ---------------------------------------------------------------- 读视图命令


def cmd_status(args) -> int:
    """status：打印单任务状态与备注。"""
    states, notes, updated = reduce_states(load_events(args.root))
    state = states.get(args.task, "pending")
    print(f"{args.task}  状态={state}  最后更新={updated.get(args.task, '-')}")
    for item in notes.get(args.task, []):
        print(f"  备注 {item}")
    return 0


def cmd_list(args) -> int:
    """list：列出任务状态（可按状态/批次过滤）。"""
    states, notes, updated = reduce_states(load_events(args.root))
    plan = parse_plan(args.plan) if args.plan else {}
    ids = sorted(
        set(plan) | set(states),
        key=lambda t: (t.split("-")[0], int(t.split("-")[1]) if t.split("-")[1].isdigit() else 0),
    )
    rows = []
    for task in ids:
        state = states.get(task, "pending")
        if args.state and state != args.state:
            continue
        if args.batch and not task.startswith(f"{args.batch}-"):
            continue
        last_note = (notes.get(task) or [""])[-1]
        rows.append((task, state, last_note, updated.get(task, "-")))
    if args.json:
        print(json.dumps(
            [{"task": t, "state": s, "note": n, "updated": u} for t, s, n, u in rows],
            ensure_ascii=False, indent=2,
        ))
        return 0
    if not rows:
        print("（无匹配任务）")
        return 0
    for task, state, note, updated in rows:
        tag = f"  {note}" if note else ""
        print(f"{MARKERS.get(state, '?')} {task:<7} {state:<8} {updated}{tag}")
    return 0


def cmd_log(args) -> int:
    """log：打印事件流（可过滤任务）。"""
    events = load_events(args.root)
    if args.task:
        events = [e for e in events if e.get("task") == args.task]
    if args.n > 0:
        events = events[-args.n:]
    if not events:
        print("（事件流为空）")
        return 0
    for e in events:
        print(
            f"#{e.get('seq'):<4} {e.get('ts', '-'):<20} {e.get('task', '-'):<7} "
            f"{str(e.get('from')):<8}→{str(e.get('to')):<8} {e.get('agent') or '-':<10} "
            f"{e.get('note') or ''}".rstrip()
        )
    return 0


def cmd_board(args) -> int:
    """board：生成看板（按批次分组，状态来自事件日志）。"""
    states, notes, updated = reduce_states(load_events(args.root))
    plan = parse_plan(args.plan)
    ids = sorted(set(plan) | set(states), key=lambda t: (t.split("-")[0], int(t.split("-")[1])))
    where = _lanes_module().lane_of(_lanes_module().parse_lanes(args.plan))
    batches: dict[str, list[str]] = {}
    for task in ids:
        batches.setdefault(task.split("-")[0], []).append(task)

    out: list[str] = []
    out.append("# 任务看板（生成物，勿手改 — 由 tools/task_state.py board 生成）")
    out.append("")
    out.append(f"生成时间：{now_iso()}　计划：{args.plan}")
    counts = {state: 0 for state in ALL_STATES}
    for task in ids:
        counts[states.get(task, "pending")] = counts.get(states.get(task, "pending"), 0) + 1
    out.append(
        "统计：" + "　".join(f"{s}={counts.get(s, 0)}" for s in ALL_STATES if counts.get(s, 0))
    )
    out.append("")
    for batch in sorted(batches):
        out.append(f"## {batch}")
        out.append("")
        out.append("| 任务 | 车道 | 状态 | 备注 | 更新 | 摘要 |")
        out.append("|---|---|---|---|---|---|")
        for task in batches[batch]:
            info = plan.get(task, {})
            state = states.get(task, "pending")
            note = "；".join(notes.get(task, [])).replace("|", "/") or "—"
            title = (info.get("title") or "").replace("|", "/")
            if len(title) > 40:
                title = title[:39] + "…"
            mark = MARKERS.get(state, "?")
            out.append(
                f"| {task} | {where.get(task, '—')} | {mark} {state} | {note} | {updated.get(task, '—')} | {title} |"
            )
        out.append("")
    text = "\n".join(out)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"✓ 看板已写入 {args.out}")
    else:
        print(text)
    return 0


def cmd_gantt_lanes(args, lanes: dict, sync: list) -> int:
    """gantt（车道版）：车道 × 推导波次；波次 = 车道顺序 + 同步点的最长路径时间槽。"""
    lanes_mod = _lanes_module()
    states, _, _ = reduce_states(load_events(args.root))
    preds = lanes_mod.predecessors(lanes, sync)
    slot, cyclic = lanes_mod.schedule(preds)
    if cyclic:
        print(f"✗ 车道依赖有环：{', '.join(sorted(set(cyclic)))}", file=sys.stderr)
        return 2
    width = max(slot.values(), default=0)
    mark = lambda t: f"{MARKERS.get(states.get(t, 'pending'), '?')}{t}"
    out: list[str] = []
    out.append("# 甘特视图（车道 × 推导波次，生成物，勿手改）")
    out.append("")
    out.append(f"生成时间：{now_iso()}　计划：{args.plan}")
    out.append("标记：" + "　".join(f"{MARKERS[s]}={s}" for s in ALL_STATES))
    out.append("")
    out.append("波次由工具推导：每个任务记 1 个单位工期，开工槽 = 车道内上一个任务与全部同步点前置之后。"
               "空格 = 该车道在这一波在等别的车道（看下方同步点）或已做完。")
    out.append("")
    header = ["车道", "agent", "进度", *[f"W{i}" for i in range(1, width + 1)]]
    out.append("| " + " | ".join(header) + " |")
    out.append("|" + "---|" * len(header))
    for lane, info in lanes.items():
        cells = [""] * width
        for t in info["tasks"]:
            if t in slot:
                cells[slot[t] - 1] = mark(t)
        done = sum(1 for t in info["tasks"] if states.get(t) == "done")
        out.append(f"| **{lane}** | {info['agent'] or '待定'} | {done}/{len(info['tasks'])} | " + " | ".join(cells) + " |")
    per_wave = [sum(1 for t in slot.values() if t == i) for i in range(1, width + 1)]
    out.append("| 并发 | | | " + " | ".join(str(n) for n in per_wave) + " |")
    out.append("")
    if sync:
        out.append("## 同步点（跨车道依赖）")
        out.append("")
        out.append("| 后（等待方） | 先（被等任务） | 原因 |")
        out.append("|---|---|---|")
        for edge in sync:
            out.append(f"| {mark(edge['after'])} | {' '.join(mark(b) for b in edge['before'])} | {edge['reason'].replace('|', '/')} |")
        out.append("")
    text = "\n".join(out)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"✓ 甘特已写入 {args.out}")
    else:
        print(text)
    return 0


def cmd_gantt(args) -> int:
    """gantt：有车道表 → 车道 × 推导波次；否则回退链 × 手写波次网格。"""
    lanes_mod = _lanes_module()
    lanes = lanes_mod.parse_lanes(args.plan)
    if lanes:
        return cmd_gantt_lanes(args, lanes, lanes_mod.parse_sync(args.plan))
    waves = parse_waves(args.plan)
    if not waves:
        print("✗ 未能从计划文档解析出波次（§4.3 格式变化？）", file=sys.stderr)
        return 2
    states, _, _ = reduce_states(load_events(args.root))

    chain_order: list[str] = []
    for wave in sorted(waves, key=lambda w: int(w[1:])):
        for chain in waves[wave]:
            if chain not in chain_order:
                chain_order.append(chain)

    wave_names = sorted(waves, key=lambda w: int(w[1:]))
    out: list[str] = []
    out.append("# 甘特视图（链 × 波次，生成物，勿手改）")
    out.append("")
    out.append(f"生成时间：{now_iso()}　计划：{args.plan}")
    out.append("标记：" + "　".join(f"{MARKERS[s]}={s}" for s in ALL_STATES))
    out.append("")
    header = ["链\\波次", *wave_names]
    out.append("| " + " | ".join(header) + " |")
    out.append("|" + "---|" * len(header))
    for chain in chain_order:
        cells = []
        for wave in wave_names:
            tasks = waves[wave].get(chain, [])
            if tasks:
                rendered = " ".join(f"{MARKERS.get(states.get(t, 'pending'), '?')}{t}" for t in tasks)
            else:
                rendered = ""
            cells.append(rendered)
        out.append(f"| **{chain}** | " + " | ".join(cells) + " |")
    out.append("")
    text = "\n".join(out)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"✓ 甘特已写入 {args.out}")
    else:
        print(text)
    return 0


def git_changed_files(repo: Path) -> list[str]:
    """工作树相对 HEAD 的全部改动（含未跟踪）的仓库相对路径。"""
    result = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    files: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        files.append(path.strip('"'))
    return files


def spec_hits_path(spec: str, path: str) -> bool:
    """判定一条「改动文件」规格是否命中某个改动路径。"""
    if spec == path:
        return True
    if any(ch in spec for ch in "*?["):
        return fnmatch.fnmatch(path, spec)
    if spec.endswith("/"):
        return path.startswith(spec) or path == spec[:-1]
    return False


def cmd_reconcile(args) -> int:
    """reconcile：plan（勾选）× 事件日志 × git 工作树 三路对账。"""
    states, _, _ = reduce_states(load_events(args.root))
    plan = parse_plan(args.plan)
    changed = git_changed_files(args.repo) if not args.no_git else []
    has_checks = any(info.get("checked") for info in plan.values())

    diffs = {
        "checked_no_done": [],   # 计划打了勾但不是 done
        "state_no_check": [],    # 有事件（active/verify/done）但计划没打勾
        "changed_unclaimed": [], # 计划中仍是 pending/claimed 却已改动其文件
        "orphan_changes": [],    # 工作树改动不属于任何任务
        "not_started": [],       # 计划内、零事件（仅计数）
    }

    id_set = sorted(set(plan) | set(states))
    for task in id_set:
        info = plan.get(task)
        state = states.get(task, "pending")
        if info and info.get("checked") and state != "done":
            diffs["checked_no_done"].append((task, state))
        if info and not info.get("checked") and state in {"active", "verify", "done"} and has_checks:
            diffs["state_no_check"].append((task, state))
        if task not in states:
            diffs["not_started"].append(task)

    declared = {t: (info.get("files") or []) for t, info in plan.items()}
    for task, files in declared.items():
        state = states.get(task, "pending")
        if state not in {"pending", "claimed"} or not files:
            continue
        hits = [p for p in changed for spec in files if spec_hits_path(spec, p)]
        if hits:
            diffs["changed_unclaimed"].append((task, ", ".join(sorted(set(hits)))))

    for path in changed:
        if not any(spec_hits_path(spec, path) for files in declared.values() for spec in files):
            diffs["orphan_changes"].append(path)

    out: list[str] = []
    out.append("# 对账报告（生成物，勿手改）")
    out.append("")
    out.append(f"生成时间：{now_iso()}　计划：{args.plan}　事件数：{len(load_events(args.root))}")

    def section(title: str, items: list, fmt) -> None:
        out.append("")
        out.append(f"## {title}（{len(items)}）")
        if not items:
            out.append("- 无")
            return
        for item in items:
            out.append(f"- {fmt(item)}")

    section("计划打勾但状态非 done（勾了无事件 / 状态不符）", diffs["checked_no_done"],
            lambda x: f"`{x[0]}` 实际 {x[1]}")
    section("有事件但计划未打勾（仅过渡期有意义）", diffs["state_no_check"],
            lambda x: f"`{x[0]}` 实际 {x[1]}")
    section("未登记却已改动其文件（pending/claimed）", diffs["changed_unclaimed"],
            lambda x: f"`{x[0]}` 命中的改动：{x[1]}")
    section("工作树改动未归属任何任务", diffs["orphan_changes"], lambda x: f"`{x}`")
    out.append("")
    out.append(f"## 计划内但零事件（未开始）：{len(diffs['not_started'])}")
    if diffs["not_started"]:
        out.append("- " + ", ".join(diffs["not_started"]))
    text = "\n".join(out)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"✓ 对账报告已写入 {args.out}")
    else:
        print(text)

    actionable = diffs["checked_no_done"] or diffs["changed_unclaimed"]
    return 1 if actionable else 0


# ---------------------------------------------------------------- CLI


WRITE_COMMANDS = {"claim", "start", "submit", "done", "block", "fail", "unblock", "revoke", "seed"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="任务状态跟踪：事件日志为唯一真源，验收是灯不是门，视图全部生成。")
    parser.add_argument("--root", type=Path, default=None, help="协调目录（默认 <git 根>/.agentic）")
    parser.add_argument("--repo", type=Path, default=None, help="仓库根（默认从 --root 向上找 .git）")
    parser.add_argument("--plan", type=Path, default=None, help="计划文档路径")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_agent(p):
        p.add_argument("--agent", required=True,
                       help="agent ID：<厂商>-<会话/实例>[/<子agent>]，如 claude-endlessspace-d5")

    def add_task(p):
        p.add_argument("task", help="任务 ID，如 P1-1")

    def add_note(p):
        p.add_argument("--note", default="", help="可选自由备注（想留痕就写，如截图/日志路径）")
        p.add_argument("--evidence", action="append",
                       help="(兼容旧用法) 证据路径，折入备注，不做任何校验")

    p = sub.add_parser("claim", help="→ claimed（认领）")
    add_task(p); add_agent(p); add_note(p)
    p.set_defaults(func=lambda a: cmd_transition(a, "claim"))

    p = sub.add_parser("start", help="→ active（开工；可从 pending/claimed 前跳）")
    add_task(p); add_agent(p); add_note(p)
    p.set_defaults(func=lambda a: cmd_transition(a, "start"))

    p = sub.add_parser("submit", help="→ verify（提交待查；可选）")
    add_task(p); add_agent(p); add_note(p)
    p.set_defaults(func=lambda a: cmd_transition(a, "submit"))

    p = sub.add_parser("done", help="→ done（无前置条件，不强制证据）")
    add_task(p); add_agent(p); add_note(p)
    p.set_defaults(func=lambda a: cmd_transition(a, "done"))

    p = sub.add_parser("block", help="任意非终态 → blocked")
    add_task(p); add_agent(p)
    p.add_argument("--reason", required=True, help="阻塞原因")
    p.set_defaults(func=cmd_block)

    p = sub.add_parser("fail", help="任意非终态 → failed")
    add_task(p); add_agent(p)
    p.add_argument("--reason", required=True, help="失败原因")
    p.set_defaults(func=cmd_fail)

    p = sub.add_parser("unblock", help="blocked → active")
    add_task(p); add_agent(p); add_note(p)
    p.set_defaults(func=lambda a: cmd_transition(a, "unblock"))

    p = sub.add_parser("revoke", help="done/failed → active（追加补偿事件，供纠错/反证）")
    add_task(p); add_agent(p)
    p.add_argument("--reason", required=True, help="撤销原因（如证据有误/测试坏了）")
    p.set_defaults(func=cmd_revoke)

    p = sub.add_parser("seed", help="从基线 JSON 灌入事件（审计收口）")
    p.add_argument("file", help="基线 JSON 路径")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("status", help="查看单任务状态与备注")
    add_task(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("list", help="列出任务状态")
    p.add_argument("--state", choices=ALL_STATES, help="只看某状态")
    p.add_argument("--batch", help="只看某批次，如 P1")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("log", help="查看事件流")
    p.add_argument("--task", help="只看某任务")
    p.add_argument("--n", type=int, default=0, help="只看最后 N 条（0=全部）")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("board", help="生成看板（Markdown）")
    p.add_argument("--out", help="写入文件；缺省打印到 stdout")
    p.set_defaults(func=cmd_board)

    p = sub.add_parser("gantt", help="生成甘特（车道 × 推导波次；无车道表时回退链 × 波次）")
    p.add_argument("--out", help="写入文件；缺省打印到 stdout")
    p.set_defaults(func=cmd_gantt)

    p = sub.add_parser("reconcile", help="plan × 事件日志 × git 三路对账（软报告）")
    p.add_argument("--out", help="写入文件；缺省打印到 stdout")
    p.add_argument("--no-git", action="store_true", help="跳过 git 工作树对比")
    p.set_defaults(func=cmd_reconcile)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.root is None:
        args.root = default_root()
    args.root = Path(args.root)
    if args.repo is None:
        args.repo = infer_repo(args.root)
    args.repo = Path(args.repo)
    if args.plan is not None:
        args.plan = Path(args.plan)
    elif args.cmd in VIEW_NAMES:
        args.plan = resolve_plan(args)
        if args.plan is None or not args.plan.is_file():
            print(f"✗ 未找到计划：传 --plan 或在 {args.root / TASKS_DIR / CONFIG_NAME} 配 plan", file=sys.stderr)
            return 2
    agent = getattr(args, "agent", None)
    if agent is not None:
        error = validate_agent_id(agent)
        if error:
            print(f"✗ {error}", file=sys.stderr)
            return 2
    code = args.func(args)
    if code == 0 and args.cmd in WRITE_COMMANDS:
        refresh_views(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
