#!/usr/bin/env python3
"""Token 账本：从 Codex / Claude Code 本地会话日志里按「轮」统计每个车道 agent 的真实 token 消耗。

数据源（只读，本机私有）：
  Codex   ~/.codex/sessions/**/rollout-*.jsonl     （token_count 事件，cwd 在本仓库的会话）
  Claude  ~/.claude/projects/<本仓库>/*.jsonl       （assistant 消息的 usage，按 message.id 去重；含子 agent）

每一轮（一次用户/唤醒输入到本轮结束）归类为：
  human   人工输入（派车道、追问）
  wake    bus_wake 自动唤醒；再按本轮有无「动作」（改文件 / commit / task_state 写 / bus send / verify）
          分 wake_work 与 wake_idle（读完就待命 = 空转）
  other   其它自动注入（如 lane_patrol 巡检、子 agent 汇报）
另外把「协调税」单列：模型调用里发出 bus.py / task_state.py / bus_wake.py 命令的那几次调用的输入。

加权口径 IET（input-equivalent tokens，≈ 按价格折成未缓存输入）：
  未缓存输入 ×1；缓存读 ×0.1；缓存写 ×1.25（Claude）；输出 ×5。只用于比较占比，不是账单。

子命令：
  report [--since 2026-10-05T00:00] [--plan plan5] [--json] [--out <md>]
         汇总表（按 agent × 轮次类型）+ 唤醒延迟 / 并行度 / 每个 done 任务的成本
  turns  [--since ...] [--agent X]     逐轮明细（jsonl）
  snapshot                              把最近两天的轮次去重并入账本 <root>/bus/metrics/turns.jsonl，
                                        并重写 token_report.md（今天）与 token_report_all.md（账本全量）。
                                        launchd com.endlessspace.token-ledger 每 30 分钟跑一次；会话日志
                                        被压缩/清理后账本仍在。
  report --ledger                       用账本而不是实时日志出报表
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bus import default_root  # noqa: E402

CODEX_SESSIONS = Path.home() / ".codex/sessions"
CLAUDE_PROJECTS = Path.home() / ".claude/projects"
AGENT_RE = re.compile(r"--(?:agent|from)\s+((?:claude|codex|trae)-[A-Za-z0-9._/-]+)")
ACTION_RE = re.compile(
    r"apply_patch|git commit|task_state\.py[^\n\"]*\b(claim|start|submit|done|block|fail|revoke)\b"
    r"|bus\.py[^\n\"]*\bsend\b|verify\.sh|\"(Edit|Write|NotebookEdit)\"", re.I)
COORD_RE = re.compile(r"bus\.py|task_state\.py|bus_wake\.py")
WAKE_MARK = "bus_wake 自动唤醒"
W_CACHED, W_CACHE_WRITE, W_OUT = 0.1, 1.25, 5.0


def ts(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def iet(t: dict) -> float:
    return (t["input"] - t["cached"] - t["cache_write"]) + W_CACHED * t["cached"] \
        + W_CACHE_WRITE * t["cache_write"] + W_OUT * t["output"]


def new_turn(agent, vendor, session, start, prompt) -> dict:
    if WAKE_MARK in prompt:
        trigger = "wake"
    elif prompt.lstrip().startswith("<") or "lane_patrol" in prompt or "巡检" in prompt[:80]:
        trigger = "other"
    else:
        trigger = "human"
    return {"agent": agent, "vendor": vendor, "session": session, "start": start, "end": start,
            "trigger": trigger, "seqs": [int(s) for s in re.findall(r"#(\d+)", prompt)][:20],
            "prompt": prompt[:160], "calls": 0, "input": 0, "cached": 0, "cache_write": 0,
            "output": 0, "coord_input": 0, "action": False}


def guess_agent(text: str, default: str) -> str:
    counts = collections.Counter(AGENT_RE.findall(text))
    return counts.most_common(1)[0][0] if counts else default


# ---- Codex -------------------------------------------------------------------

def codex_turns(repo: Path, since: float, names: dict[str, str]) -> list[dict]:
    out = []
    for path in sorted(CODEX_SESSIONS.glob("*/*/*/rollout-*.jsonl")):
        if path.stat().st_mtime < since:
            continue
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not lines or str(repo) not in lines[0]:
            continue
        thread = path.stem[-36:]
        agent = names.get(thread) or guess_agent("\n".join(lines), f"codex-?{thread[:8]}")
        cur = None
        pending_coord = False
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            p = e.get("payload") or {}
            kind = p.get("type")
            if kind == "task_started":
                cur = new_turn(agent, "codex", thread, ts(e.get("timestamp")), "")
                cur["_need_prompt"] = True
                out.append(cur)
                continue
            if cur is None:
                continue
            cur["end"] = ts(e.get("timestamp")) or cur["end"]
            if e.get("type") == "response_item" and kind == "message" and p.get("role") == "user" \
                    and cur.pop("_need_prompt", False):
                prompt = "".join(c.get("text", "") for c in p.get("content", []))
                fresh = new_turn(agent, "codex", thread, cur["start"], prompt)
                cur.update({k: fresh[k] for k in ("trigger", "seqs", "prompt")})
            elif e.get("type") == "response_item" and kind in ("custom_tool_call", "function_call"):
                blob = json.dumps(p, ensure_ascii=False)
                cur["action"] |= bool(ACTION_RE.search(blob))
                pending_coord |= bool(COORD_RE.search(blob))
            elif kind == "token_count" and p.get("info"):
                u = p["info"]["last_token_usage"]
                cur["calls"] += 1
                cur["input"] += u.get("input_tokens", 0)
                cur["cached"] += u.get("cached_input_tokens", 0)
                cur["output"] += u.get("output_tokens", 0)
                if pending_coord:  # 发出协调命令的那次调用
                    cur["coord_input"] += u.get("input_tokens", 0) - u.get("cached_input_tokens", 0) \
                        + W_CACHED * u.get("cached_input_tokens", 0)
                    pending_coord = False
    return [t for t in out if t["end"] >= since]


# ---- Claude Code -------------------------------------------------------------

def claude_turns(repo: Path, since: float) -> list[dict]:
    project = CLAUDE_PROJECTS / ("-" + str(repo).strip("/").replace("/", "-").replace("_", "-"))
    out = []
    for path in sorted(project.glob("*.jsonl")) + sorted(project.glob("*/subagents/*.jsonl")):
        if path.stat().st_mtime < since:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        session = path.stem
        sub = "/subagents/" in str(path)
        agent = guess_agent(text, f"claude-?{session[:8]}")
        if sub:
            agent += "/sub"
        cur = None
        seen_ids: set[str] = set()
        for line in text.splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            kind = e.get("type")
            msg = e.get("message") or {}
            when = ts(e.get("timestamp"))
            if kind == "user":
                content = msg.get("content")
                if isinstance(content, str) or (isinstance(content, list) and content
                                                 and all(c.get("type") == "text" for c in content)):
                    prompt = content if isinstance(content, str) else "".join(c.get("text", "") for c in content)
                    cur = new_turn(agent, "claude", session, when, prompt)
                    out.append(cur)
                elif cur is not None:
                    cur["end"] = when or cur["end"]
            elif kind == "assistant" and cur is not None:
                cur["end"] = when or cur["end"]
                blob = json.dumps(msg.get("content"), ensure_ascii=False)
                cur["action"] |= bool(ACTION_RE.search(blob))
                mid = msg.get("id")
                u = msg.get("usage")
                if not u or mid in seen_ids:
                    if COORD_RE.search(blob) and cur.get("_last"):
                        cur["coord_input"] += cur.pop("_last")
                    continue
                seen_ids.add(mid)
                read, write, fresh = (u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0),
                                      u.get("input_tokens", 0))
                cur["calls"] += 1
                cur["input"] += fresh + read + write
                cur["cached"] += read
                cur["cache_write"] += write
                cur["output"] += u.get("output_tokens", 0)
                call_cost = fresh + W_CACHED * read + W_CACHE_WRITE * write
                if COORD_RE.search(blob):
                    cur["coord_input"] += call_cost
                else:
                    cur["_last"] = call_cost
    for t in out:
        t.pop("_last", None)
    return [t for t in out if t["end"] >= since and t["calls"]]


# ---- 汇总 --------------------------------------------------------------------

def classify(t: dict) -> str:
    if t["trigger"] == "wake":
        return "wake_work" if t["action"] else "wake_idle"
    return t["trigger"]


def collect(root: Path, since: float, plan: str | None) -> list[dict]:
    repo = root.resolve().parent
    wake_map = {}
    with_map = root / "bus" / "wake_map.json"
    if with_map.is_file():
        wake_map = {v: k for k, v in json.loads(with_map.read_text(encoding="utf-8")).items()}
    turns = codex_turns(repo, since, wake_map) + claude_turns(repo, since)
    if plan:
        agents = plan_agents(root, plan)
        turns = [t for t in turns if t["agent"].split("/")[0] in agents]
    for t in turns:
        t["kind"] = classify(t)
        t["iet"] = iet(t)
    return sorted(turns, key=lambda t: t["start"])


def plan_agents(root: Path, plan: str) -> set[str]:
    import lanes as lanes_mod
    import plans as plans_mod
    doc = plans_mod.plan_dir(root, plan) / plans_mod.DOC_NAME
    agents = {lane["agent"] for lane in lanes_mod.parse_lanes(doc).values() if lane["agent"]}
    return agents | set(plans_mod.COORD_RE.findall(doc.read_text(encoding="utf-8")))


def wake_latency(root: Path, turns: list[dict]) -> list[float]:
    """唤醒轮开始时间 − 触发它的最早消息时间（秒）：消息到被处理的真实等待。"""
    log = root / "bus" / "log.jsonl"
    sent = {}
    if log.is_file():
        for line in log.read_text(encoding="utf-8").splitlines():
            with_seq = json.loads(line)
            sent[with_seq.get("seq")] = ts(with_seq.get("ts"))
    out = []
    for t in turns:
        stamps = [sent[s] for s in t["seqs"] if s in sent]
        if t["trigger"] == "wake" and stamps:
            out.append(max(0.0, t["start"] - min(stamps)))
    return out


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def done_count(root: Path, plan: str | None, since: float) -> int:
    import plans as plans_mod
    n = 0
    for name, pdir, _ in plans_mod.all_plans(root):
        if plan and name != plan:
            continue
        for line in (pdir / "events.jsonl").read_text(encoding="utf-8").splitlines() if (pdir / "events.jsonl").is_file() else []:
            e = json.loads(line)
            if e.get("to") == "done" and ts(e.get("ts")) >= since:
                n += 1
    return n


def report(root: Path, turns: list[dict], plan: str | None, since: float) -> str:
    kinds = ["human", "wake_work", "wake_idle", "other"]
    total = sum(t["iet"] for t in turns) or 1
    by_kind = collections.defaultdict(list)
    for t in turns:
        by_kind[t["kind"]].append(t)
    lines = [f"# Token 账本{'（' + plan + '）' if plan else ''}",
             "",
             f"统计自 {datetime.fromtimestamp(since).strftime('%Y-%m-%d %H:%M') if since else '全部'}；"
             f"{len(turns)} 轮，{len({t['agent'] for t in turns})} 个 agent。IET = 按价格折算的输入当量（见 token_usage.py 头注释）。",
             "",
             "## 按轮次类型",
             "",
             "| 类型 | 轮数 | 调用/轮 | 原始输入 | 输出 | IET | IET 占比 | IET/轮 |",
             "|---|---|---|---|---|---|---|---|"]
    for k in kinds:
        ts_ = by_kind.get(k, [])
        if not ts_:
            continue
        s = sum(t["iet"] for t in ts_)
        lines.append(f"| {k} | {len(ts_)} | {sum(t['calls'] for t in ts_) / len(ts_):.1f} | "
                     f"{sum(t['input'] for t in ts_) / 1e6:.1f}M | {sum(t['output'] for t in ts_) / 1e3:.0f}k | "
                     f"{s / 1e6:.2f}M | {s / total:.0%} | {s / len(ts_) / 1e3:.0f}k |")
    coord = sum(t["coord_input"] for t in turns)
    idle = sum(t["iet"] for t in by_kind.get("wake_idle", []))
    wakes = len(by_kind.get("wake_work", [])) + len(by_kind.get("wake_idle", []))
    lines += ["",
              "## 协作开销",
              "",
              f"- 空转唤醒：{len(by_kind.get('wake_idle', []))}/{wakes} 次唤醒，占总 IET {idle / total:.1%}",
              f"- 协调命令调用（bus / task_state / bus_wake 那几次模型调用的输入）：占总 IET {coord / total:.1%}",
              f"- 协作总税（空转 + 非空转轮里的协调调用）：约 {(idle + coord - sum(t['coord_input'] for t in by_kind.get('wake_idle', []))) / total:.1%}"]
    busy = sum(max(0.0, t["end"] - t["start"]) for t in turns if "/sub" not in t["agent"])
    span = (max(t["end"] for t in turns) - min(t["start"] for t in turns)) if turns else 0
    done = done_count(root, plan, since)
    lat = wake_latency(root, turns)
    lines += ["",
              "## 速度与产出",
              "",
              f"- 墙钟跨度 {span / 3600:.1f} h，各 agent 忙碌时间合计 {busy / 3600:.1f} h → 平均并行度 {busy / span if span else 0:.1f}×",
              f"- 期间 done 事件 {done} 个 → 每个 done 约 {total / done / 1e6:.2f}M IET" if done else "- 期间无 done 事件",
              f"- 唤醒延迟（消息发出→被唤醒轮开始）：中位 {pct(lat, .5):.0f}s，P90 {pct(lat, .9):.0f}s（n={len(lat)}）",
              f"- 干活唤醒轮时长：中位 {pct([t['end'] - t['start'] for t in by_kind.get('wake_work', [])], .5) / 60:.1f} min；"
              f"人工轮时长中位 {pct([t['end'] - t['start'] for t in by_kind.get('human', [])], .5) / 60:.1f} min"]
    lines += ["", "## 按 agent", "", "| agent | 厂商 | 轮数 | 空转唤醒 | IET | 占比 |", "|---|---|---|---|---|---|"]
    per = collections.defaultdict(list)
    for t in turns:
        per[t["agent"]].append(t)
    for agent, ts_ in sorted(per.items(), key=lambda kv: -sum(t["iet"] for t in kv[1])):
        s = sum(t["iet"] for t in ts_)
        lines.append(f"| {agent} | {ts_[0]['vendor']} | {len(ts_)} | {sum(t['kind'] == 'wake_idle' for t in ts_)} | "
                     f"{s / 1e6:.2f}M | {s / total:.0%} |")
    return "\n".join(lines) + "\n"


def ledger_path(root: Path) -> Path:
    return root / "bus" / "metrics" / "turns.jsonl"


def load_ledger(root: Path) -> list[dict]:
    path = ledger_path(root)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def snapshot(root: Path) -> int:
    """最近 2 天的轮次覆盖进账本（同一轮以 会话+开始时间 为键，后到的数据覆盖进行中的旧值）。"""
    now = datetime.now().timestamp()
    fresh = collect(root, now - 2 * 86400, None)
    merged = {(t["session"], t["start"]): t for t in load_ledger(root)}
    merged.update({(t["session"], t["start"]): t for t in fresh})
    turns = sorted(merged.values(), key=lambda t: t["start"])
    path = ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in turns), encoding="utf-8")
    tmp.replace(path)
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    (path.parent / "token_report.md").write_text(
        report(root, [t for t in turns if t["end"] >= today], None, today), encoding="utf-8")
    (path.parent / "token_report_all.md").write_text(report(root, turns, None, 0.0), encoding="utf-8")
    print(f"✓ 账本 {len(turns)} 轮（本次扫描 {len(fresh)} 轮）→ {path.parent}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Token 账本：按轮统计车道 agent 的真实 token 消耗。")
    parser.add_argument("--root", type=Path, default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("snapshot")
    for name in ("report", "turns"):
        p = sub.add_parser(name)
        p.add_argument("--ledger", action="store_true", help="读账本而不是实时会话日志")
        p.add_argument("--since", default=None, help="ISO 时间，如 2026-10-05T00:00（本地时区）")
        p.add_argument("--plan", default=None, help="只统计该计划车道表里的 agent + 协调人")
        p.add_argument("--agent", default=None)
        p.add_argument("--json", action="store_true")
        p.add_argument("--out", default=None, help="report 写入文件（如 .agentic/metrics/token_report.md）")
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else default_root()
    if args.cmd == "snapshot":
        return snapshot(root)
    since = datetime.fromisoformat(args.since).timestamp() if args.since else 0.0
    if args.ledger:
        agents = plan_agents(root, args.plan) if args.plan else None
        turns = [t for t in load_ledger(root) if t["end"] >= since
                 and (agents is None or t["agent"].split("/")[0] in agents)]
    else:
        turns = collect(root, since, args.plan)
    if args.agent:
        turns = [t for t in turns if t["agent"].startswith(args.agent)]
    if args.cmd == "turns" or args.json:
        for t in turns:
            print(json.dumps(t, ensure_ascii=False))
        return 0
    text = report(root, turns, args.plan, since)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"✓ 已写 {args.out}")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
