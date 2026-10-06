#!/usr/bin/env python3
"""唤醒路由：决定一条总线消息值不值得叫醒某个 agent（纯规则，不调模型）。

统计（plan5，2026-10-05）：Codex 车道 205 次唤醒里 184 次是空转（读完回「继续待命」），
主因是广播 DONE / RELEASE / CLAIM 不分对象叫醒所有车道。本模块按计划的车道表 + 同步点
把广播路由到真正受影响的 agent：

- 点对点消息：相关；ACCEPT 回执不单独叫醒，CONFLICT 同一 发送方/任务 只在第一次相关。
- 广播正文点名了该 agent：相关。
- 广播 DONE <任务>：只叫醒「直接后继未 done 且前置已全部 done（真正解锁）」的车道 agent 与协调人。
- 广播 BLOCKED / REQUEST_HELP <任务>：叫醒协调人与该任务前置任务的持有车道。
- 广播 CLAIM / RELEASE / ACCEPT / CONFLICT：不叫醒（只落日志，下次自然 read 时看到）。
- 广播 INTERRUPT / PROPOSE / HANDOFF 或无法解析任务：叫醒协调人与车道未做完的 agent。
- 车道已全部 done 的 agent：只收点对点和点名。
- 协调人（1e）：DONE 不逐条叫醒——自动验收 pass 的略过，其余进验收队列，攒满 ACCEPT_BATCH 条
  或最早一条等满 ACCEPT_MAX_WAIT 才叫醒（1b）；其余非静默广播与点对点照常。

条件唤醒（wake_conditions.json）：agent 用 `bus_wake.py until --task X` 登记在等 X，
登记期间只收点对点/点名/INTERRUPT；X 进入目标状态时守护进程直接叫醒它并清掉条件。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

CONDITIONS_NAME = "wake_conditions.json"
TASK_TOKEN = re.compile(r"\b[A-Z]+\d+-\d+\b")  # 与 lanes.TASK_TOKEN 同口径
QUIET_BROADCAST = {"CLAIM", "RELEASE", "ACCEPT", "CONFLICT"}
DONE_STATES = {"done", "migrated"}
BODY_PREVIEW = 300


class PlanView:
    """所有 active 计划合并后的车道 / 依赖 / 状态快照（一次唤醒决策内复用）。"""

    def __init__(self, root: Path):
        import lanes as lanes_mod
        import plans as plans_mod
        from task_state import EVENTS_NAME, iter_jsonl, reduce_states

        self.owner: dict[str, str] = {}          # 任务 → 车道 agent
        self.preds: dict[str, set[str]] = {}     # 任务 → 直接前置
        self.succs: dict[str, set[str]] = {}     # 任务 → 直接后继
        self.states: dict[str, str] = {}
        self.coords: set[str] = set()
        self.agent_tasks: dict[str, list[str]] = {}
        self.auto: dict[str, str] = {}           # 任务 → 最近一次 done 的自动验收结论（pass / fail）
        for _, pdir, _ in plans_mod.active_plans(root):
            doc = pdir / plans_mod.DOC_NAME
            if not doc.is_file():
                continue
            lanes = lanes_mod.parse_lanes(doc)
            for info in lanes.values():
                for task in info["tasks"]:
                    if info["agent"]:
                        self.owner[task] = info["agent"]
                        self.agent_tasks.setdefault(info["agent"], []).append(task)
            for task, before in lanes_mod.predecessors(lanes, lanes_mod.parse_sync(doc)).items():
                self.preds.setdefault(task, set()).update(before)
                for b in before:
                    self.succs.setdefault(b, set()).add(task)
            self.coords |= set(plans_mod.COORD_RE.findall(doc.read_text(encoding="utf-8")))
            events = list(iter_jsonl(pdir / EVENTS_NAME))
            states, _, _ = reduce_states(events)
            self.states.update(states)
            for event in events:
                if event.get("to") == "done" and event.get("task"):
                    self.auto[event["task"]] = event.get("auto", "")

    def lane_finished(self, agent: str) -> bool:
        tasks = self.agent_tasks.get(agent)
        return bool(tasks) and all(self.states.get(t) in DONE_STATES for t in tasks)

    def unlocked(self, task: str) -> bool:
        """前置（车道内上一个 + 同步点）是否已全部 done。"""
        return all(self.state(p) in DONE_STATES for p in self.preds.get(task, ()))

    def state(self, task: str) -> str:
        return self.states.get(task, "pending")


def load_view(root: Path) -> PlanView | None:
    try:
        return PlanView(root)
    except Exception:  # noqa: BLE001  计划解析失败时退回「全都相关」，宁多叫不漏叫
        return None


DEFER = "待验收"
ACCEPT_BATCH = 3                 # 1b：协调人验收队列攒满几条叫醒一次
ACCEPT_MAX_WAIT = 20 * 60        # 1b：最早一条最多等多久（秒）


def msg_tasks(msg: dict) -> list[str]:
    found = [msg["task_id"]] if msg.get("task_id") else []
    return found + [t for t in TASK_TOKEN.findall(msg.get("body") or "") if t not in found]


def relevance(view: PlanView | None, agent: str, msg: dict, seen_conflicts: set | None = None) -> str | None:
    """相关就返回一句原因（写进唤醒摘要），不相关返回 None。"""
    mtype = msg.get("type")
    if view is not None and agent in view.coords and mtype == "DONE" and msg.get("from") != agent:
        # 1a/1e：协调人不再被逐条 DONE 叫醒——自动验收通过的直接略过，其余进验收队列（1b 攒批）
        tasks = msg_tasks(msg)
        if tasks and all(view.auto.get(t) == "pass" for t in tasks):
            return None
        if msg.get("to") in (agent, "*"):
            return DEFER
        return None
    if msg.get("to") == agent:
        if mtype == "ACCEPT":
            return None  # 回执：下次醒来时随摘要看到，不单独叫醒
        if mtype == "CONFLICT" and seen_conflicts is not None:
            key = (msg.get("from"), msg.get("task_id"))
            if key in seen_conflicts:
                return None
            seen_conflicts.add(key)
        return "点对点"
    if msg.get("to") != "*" or msg.get("from") == agent:
        return None
    if agent in (msg.get("body") or ""):
        return "广播点名你"
    if view is None:
        return "广播（计划解析失败，按相关处理）"
    if mtype in QUIET_BROADCAST:
        return None
    is_coord = agent in view.coords
    if view.lane_finished(agent) and not is_coord:
        return None
    tasks = msg_tasks(msg)
    if mtype == "DONE" and tasks:
        hits = [s for t in tasks for s in sorted(view.succs.get(t, ()))
                if view.owner.get(s) == agent and view.state(s) not in DONE_STATES and view.unlocked(s)]
        return f"解锁你的 {'、'.join(dict.fromkeys(hits))}" if hits else None
    if mtype in ("BLOCKED", "REQUEST_HELP") and tasks:
        if is_coord:
            return "协调人"
        hits = [p for t in tasks for p in sorted(view.preds.get(t, ())) if view.owner.get(p) == agent]
        return f"在等你的 {'、'.join(dict.fromkeys(hits))}" if hits else None
    if is_coord:
        return "协调人"
    if agent not in view.agent_tasks:
        return None
    return "全员广播"


# ---- 条件唤醒 ----------------------------------------------------------------

def conditions_path(root: Path) -> Path:
    from bus import bus_dir
    return bus_dir(root) / CONDITIONS_NAME


def load_conditions(root: Path) -> dict[str, dict]:
    path = conditions_path(root)
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        return {}


def save_conditions(root: Path, data: dict) -> None:
    path = conditions_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def condition_met(view: PlanView | None, cond: dict) -> bool:
    if view is None:
        return False
    want = cond.get("state", "done")
    return all(view.state(t) == want or (want == "done" and view.state(t) in DONE_STATES)
               for t in cond.get("tasks", []))


def gated(cond: dict | None, msg: dict, agent: str) -> bool:
    """登记了条件的 agent：只放行点对点、点名、INTERRUPT。"""
    if not cond:
        return False
    return not (msg.get("to") == agent or msg.get("type") == "INTERRUPT" or agent in (msg.get("body") or ""))


# ---- 摘要 --------------------------------------------------------------------

def digest(relevant: list[tuple[dict, str]], skipped: list[dict]) -> str:
    lines = []
    for msg, why in relevant:
        body = (msg.get("body") or "").replace("\n", " ")
        if why != "点对点" and len(body) > BODY_PREVIEW:  # 点对点全文给出，广播只给预览
            body = body[:BODY_PREVIEW] + "…"
        plan = f"{msg['plan']} " if msg.get("plan") else ""
        task = f" task={msg['task_id']}" if msg.get("task_id") else ""
        lines.append(f"- {plan}#{msg.get('seq')} {msg.get('type')} {msg.get('from')}→{msg.get('to')}{task}（{why}）：{body}")
    if skipped:
        counts: dict[str, int] = {}
        for msg in skipped:
            counts[msg.get("type", "?")] = counts.get(msg.get("type", "?"), 0) + 1
        kinds = "、".join(f"{k}×{v}" for k, v in counts.items())
        lines.append(f"- 另有 {len(skipped)} 条与你无关的消息已略（{kinds}），需要可 `bus.py tail` 查看")
    return "\n".join(lines)
