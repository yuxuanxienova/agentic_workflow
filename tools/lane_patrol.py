#!/usr/bin/env python3
"""车道巡检·找出「不发消息就没人知道」的卡顿（通用 Agentic Workflow 版）。

bus_wake 只在有消息时叫醒会话；车道若卡住却不发消息，协调人永远不会被叫醒。本工具补这个盲区，
由 launchd 定时跑（建议 30 分钟）。只读检查，**有问题才**给协调人发一条总线消息（会自动叫醒协调人），
没问题静默退出，不花 token。

检查项（每个 active 计划、每条车道）：
  无人      车道还有未终结任务，但它的 agent 没有在线会话（Claude 自动发现 / Codex wake_map）。
  挂起      任务进入 active/verify 已超过 --stale 分钟，负责它的会话此刻空闲（按任务计时，不受被唤醒回一句
            「待命」之类的留痕重置）；附上它登记的条件唤醒与最后一条相关消息，方便协调人判断在等谁。
  停滞      车道有 active/verify 任务，会话空闲，且该 agent 超过 --stale 分钟没有任何事件/总线消息。
  空转      车道没有进行中的任务、下一个任务（车道顺序第一个未终结）同步点已满足，会话空闲且超时。
  可调序    下一个任务卡在别的车道，但后面有同步点已满足的任务，会话空闲且超时（交协调人判断）。
  可解锁    任务处于 blocked，但它的同步点前置已全部 done（多半在等一条没人发的 DONE）。

同样的发现不重复报：结果摘要记在 <root>/bus/patrol_state.json，变化或超过 --repeat 分钟才再报。

用法：
  lane_patrol.py [--stale 30] [--repeat 120] [--dry-run]
退出码：0 无发现或已报告；2 用法错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lanes as lanes_mod  # noqa: E402
import plans as plans_mod  # noqa: E402
from bus import default_root, iter_jsonl, log_path  # noqa: E402
import project_config  # noqa: E402

STATE_NAME = "patrol_state.json"
TERMINAL = {"done", "failed", "migrated"}
IN_PROGRESS = {"active", "verify"}


def parse_ts(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def session_status(root: Path) -> dict[str, str]:
    """{agent: idle/busy/active/notLoaded…}；拿不到的 agent 不在表里（=无在线会话）。"""
    import bus_wake
    status: dict[str, str] = {}
    for agent, meta in bus_wake.claude_sessions(root.resolve().parent).items():
        status[agent] = str(meta.get("status", "?"))
    mapping = bus_wake.load_json(root / "bus" / bus_wake.MAP_NAME)
    if mapping:
        try:
            server = bus_wake.AppServer()
            for agent, thread in mapping.items():
                try:
                    status[agent] = bus_wake.thread_status(server, thread)
                except RuntimeError:
                    pass
        except SystemExit:
            pass  # daemon 不在：Codex 状态未知，不当作无人
            for agent in mapping:
                status.setdefault(agent, "unknown")
    return status


def last_activity(root: Path) -> dict[str, datetime]:
    """每个 agent 最近一次留痕（任意计划的状态事件或它发出的总线消息）。"""
    seen: dict[str, datetime] = {}

    def note(agent: str | None, ts: str | None) -> None:
        when = parse_ts(ts)
        if agent and when and (agent not in seen or when > seen[agent]):
            seen[agent] = when
    for _, pdir, _ in plans_mod.active_plans(root):
        for event in iter_jsonl(pdir / "events.jsonl"):
            note(event.get("agent"), event.get("ts"))
    for message in iter_jsonl(log_path(root)):
        note(message.get("from"), message.get("ts"))
    # 会话本身在干活（跑测试、改代码）但没发消息也算留痕：取对话记录最后修改时间
    try:
        import bus_wake
        for agent, meta in bus_wake.claude_sessions(root.resolve().parent).items():
            for transcript in bus_wake.CLAUDE_PROJECTS.glob(f"*/{meta.get('sessionId')}.jsonl"):
                note(agent, datetime.fromtimestamp(transcript.stat().st_mtime, timezone.utc).isoformat())
    except Exception:  # noqa: BLE001  拿不到就只看总线/事件
        pass
    return seen


def waiting_hint(root: Path, agent: str, task: str) -> str:
    """它登记的条件唤醒 + 它最后一条提到该任务的总线消息（截断），帮协调人判断在等谁。"""
    parts = []
    try:
        import wake_routing
        cond = wake_routing.load_conditions(root).get(agent)
        if cond:
            parts.append(f"登记在等 {'、'.join(cond.get('tasks', []))} → {cond.get('state', 'done')}")
    except Exception:  # noqa: BLE001
        pass
    by_id = by_body = None  # 优先 task_id 就是该任务的消息，没有才退到正文提及
    for message in iter_jsonl(log_path(root)):
        if message.get("from") != agent:
            continue
        if message.get("task_id") == task:
            by_id = message
        elif task in (message.get("body") or ""):
            by_body = message
    last = by_id or by_body
    if last:
        body = (last.get("body") or "").replace("\n", " ")
        parts.append(f"最后说（#{last.get('seq')} {last.get('type')}）：{body[:120]}{'…' if len(body) > 120 else ''}")
    return "；" + "；".join(parts) if parts else ""


def plan_findings(root: Path, now: datetime, stale_min: int, status: dict[str, str],
                  activity: dict[str, datetime]) -> list[str]:
    findings: list[str] = []
    for name, pdir, _ in plans_mod.active_plans(root):
        doc = pdir / plans_mod.DOC_NAME
        if not doc.is_file():
            continue
        lanes = lanes_mod.parse_lanes(doc)
        sync_preds: dict[str, set[str]] = {}
        for edge in lanes_mod.parse_sync(doc):
            sync_preds.setdefault(edge["after"], set()).update(edge["before"])
        states: dict[str, str] = {}
        since: dict[str, datetime] = {}  # 任务进入当前状态的时间
        for event in iter_jsonl(pdir / "events.jsonl"):
            if event.get("task") and event.get("to"):
                if states.get(event["task"]) != event["to"]:
                    since[event["task"]] = parse_ts(event.get("ts")) or now
                states[event["task"]] = event["to"]
        done = {t for t, s in states.items() if s in TERMINAL}

        def ready(task: str) -> bool:
            # 只看跨车道同步点；车道内顺序可能经协调人批准调过序
            return sync_preds.get(task, set()) <= done

        for lane, info in lanes.items():
            agent = info.get("agent")
            open_tasks = [t for t in info["tasks"] if states.get(t, "pending") not in TERMINAL]
            if not agent or not open_tasks:
                continue
            tag = f"{name}/{lane}（{agent}）"
            sess = status.get(agent)
            idle = sess in ("idle", "notLoaded")
            last = activity.get(agent)
            quiet = last is None or (now - last).total_seconds() > stale_min * 60
            quiet_text = "从未留痕" if last is None else f"{int((now - last).total_seconds() // 60)} 分钟无留痕"
            if sess is None:
                findings.append(f"无人：{tag} 还有 {len(open_tasks)} 个未终结任务（{', '.join(open_tasks[:4])}…），没有在线会话")
                continue
            in_progress = [t for t in open_tasks if states.get(t) in IN_PROGRESS]
            hung = [t for t in in_progress if idle and since.get(t)
                    and (now - since[t]).total_seconds() > stale_min * 60]
            for task in hung:
                minutes = int((now - since[task]).total_seconds() // 60)
                findings.append(f"挂起：{tag} 的 {task} 已 {states[task]} {minutes} 分钟，会话空闲"
                                f"{waiting_hint(root, agent, task)}")
            if in_progress and idle and quiet and not hung:
                findings.append(f"停滞：{tag} 有进行中任务 {', '.join(in_progress)}，会话空闲且{quiet_text}")
            for task in open_tasks:
                if states.get(task) == "blocked" and sync_preds.get(task) and ready(task):
                    findings.append(f"可解锁：{tag} 的 {task} 为 blocked，但同步点前置 "
                                    f"{', '.join(sorted(sync_preds[task]))} 已全部 done")
            if not in_progress and idle and quiet:
                head = open_tasks[0]
                if ready(head) and states.get(head, "pending") != "blocked":
                    findings.append(f"空转：{tag} 下一个任务 {head} 的同步点已满足，会话空闲且{quiet_text}")
                else:
                    later = [t for t in open_tasks[1:] if ready(t) and states.get(t, "pending") != "blocked"]
                    if later:
                        waits = ", ".join(sorted(sync_preds.get(head, set()) - done)) or "车道内前序"
                        findings.append(f"可调序：{tag} 卡在 {head}（等 {waits}），但 {', '.join(later[:3])} 同步点已满足；"
                                        f"会话空闲且{quiet_text}——协调人判断文件不重叠可批准先做")
    return findings


def coordinators(root: Path, cfg: dict) -> list[str]:
    """active 计划文档里「协调 `<ID>`」写明的协调人；都没写时退回 config.json 的 coordinator。"""
    found: set[str] = set()
    for _, pdir, _ in plans_mod.active_plans(root):
        doc = pdir / plans_mod.DOC_NAME
        if doc.is_file():
            found |= set(plans_mod.COORD_RE.findall(doc.read_text(encoding="utf-8")))
    return sorted(found) or [cfg["coordinator"]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="车道巡检：找出不发消息的卡顿，有问题才通知协调人。")
    parser.add_argument("--root", type=Path, default=None, help="协调目录（默认 <git 根>/.agentic）")
    parser.add_argument("--stale", type=int, default=30, help="多少分钟无留痕算停滞（默认 30）")
    parser.add_argument("--repeat", type=int, default=120, help="同样的发现隔多少分钟再报（默认 120）")
    parser.add_argument("--to", default=None, help="报告收件人（默认：active 计划里写明的协调人，没有则用 config.json 的 coordinator）")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不发总线消息")
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else default_root()
    now = datetime.now(timezone.utc)

    findings = plan_findings(root, now, args.stale, session_status(root), last_activity(root))
    stamp = now.strftime("%H:%M")
    if not findings:
        print(f"{stamp} 巡检：无发现")
        return 0
    body = f"车道巡检（{len(findings)} 项）：\n" + "\n".join(f"- {line}" for line in findings)
    print(f"{stamp} {body}")
    if args.dry_run:
        return 0

    state_path = root / "bus" / STATE_NAME
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    digest = hashlib.sha1("\n".join(sorted(findings)).encode()).hexdigest()
    last = parse_ts(state.get("ts"))
    if state.get("digest") == digest and last and (now - last).total_seconds() < args.repeat * 60:
        print("（与上次相同，未过重复间隔，不再通知）")
        return 0
    cfg = project_config.load(root)
    for to in [args.to] if args.to else coordinators(root, cfg):
        subprocess.run([sys.executable, str(Path(__file__).with_name("bus.py")), "--root", str(root), "send",
                        "--from", cfg["patrol_id"], "--to", to, "--type", "INTERRUPT", "--body", body], check=False)
    state_path.write_text(json.dumps({"digest": digest, "ts": now.isoformat(timespec="seconds")}) + "\n",
                          encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
