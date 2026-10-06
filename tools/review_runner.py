#!/usr/bin/env python3
"""验收 agent：task_state done 进队列，攒批后起一个全新上下文的 Claude Sonnet 会话一次验收一批。

流程：
  task_state done  → enqueue（<root>/bus/review_queue.json）
  bus_wake watch   → 每轮 due()：队列 ≥ BATCH 条或最早一条等满 MAX_WAIT 秒，且没有验收在跑 → 后台起 run
  run              → 取整批 → 每个任务 `task_state packet` 拼成材料 + AGENTS.md 验收铁律 →
                     `claude -p --model claude-sonnet-5-5 --effort low --json-schema ...`（只读 + git + 可在临时
                     worktree 跑测试；禁 Edit/Write）→ 按结论：
                       pass   → task_state review pass，给车道发 ACCEPT（回执，不叫醒）
                       reject → task_state review reject（done 退回 active），给车道发 REJECT（叫醒车道去修）
                       unsure → task_state review unsure，给协调人发 REQUEST_HELP
  失败（超时 / 解析不了）→ 整批放回队列；同一任务连续失败 MAX_ATTEMPTS 次转协调人。
  每批的耗时、费用、token 记到 <root>/bus/metrics/reviews.jsonl。

子命令：
  status                 队列与运行状态
  run [--force]          立即验收当前队列（--force 不管是否攒够）
  enqueue --plan P --task T --agent A    手动入队（task_state done 会自动做）
  trial --plan P --task A,B,C            试跑：对指定任务出结论但不写回、不发消息（校准用）
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bus import bus_dir, default_root  # noqa: E402
import project_config  # noqa: E402

MODEL = os.environ.get("AGENTIC_REVIEW_MODEL", "claude-sonnet-5-5")
EFFORT = os.environ.get("AGENTIC_REVIEW_EFFORT", "low")
BATCH = 3                    # 攒满几条验收一次
MAX_WAIT = 20 * 60           # 最早一条最多等多久（秒）
MAX_BATCH = 6                # 一批最多几个任务（太多材料会稀释注意力）
MAX_ATTEMPTS = 2             # 同一任务验收失败几次后转协调人
RUN_TIMEOUT = 25 * 60        # 单批上限（含跑一遍测试）；偶发卡死时尽快重试
BUDGET_USD = os.environ.get("AGENTIC_REVIEW_BUDGET_USD", "5")
TOOLS = Path(__file__).resolve().parent


def claude_bin() -> str:
    """launchd 的 PATH 不含 ~/.local/bin：显式找 claude（AGENTIC_CLAUDE_BIN 可覆盖）。"""
    import shutil
    for candidate in (os.environ.get("AGENTIC_CLAUDE_BIN"), shutil.which("claude"),
                      str(Path.home() / ".local/bin/claude"), "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        if candidate and Path(candidate).is_file():
            return candidate
    return "claude"

SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["pass", "reject", "unsure"]},
                    "reason": {"type": "string"},
                    "fix": {"type": "string"},
                    "tests_run": {"type": "string"},
                },
                "required": ["task", "verdict", "reason"],
            },
        }
    },
    "required": ["results"],
}

INSTRUCTIONS = """你是 {project} 的验收 agent（{reviewer}）。下面是一批已经 done 的任务，逐个验收，按 JSON schema 输出结论。

验收要点：
- 提交是否实现了规格（对照「规格」与「登记改动文件」），有没有明显 bug、回归、范围外改动。
- 备注写的验收等级是否有依据（{levels}）；
  「L2 待」「L1 待」这类如实写明的缺口不算打回理由，备注造假或等级没依据才算。
- 机械检查提示（如果有）列出的问题是否真的存在。

工作约束：
- 主工作树只读：不得编辑文件、不得 git add / commit / checkout / stash / reset，其它 agent 在同一工作树里干活。
{test_block}- 判断标准：pass = 实现了规格且没发现代码/证据问题；reject = 代码或验收证据有具体、可修的问题（fix 里写清楚要改什么）；
  计划登记遗漏、备注措辞这类纯文书问题不打回（打回会让车道返工一轮），判 pass 并在 reason 里注明；
  unsure = 需要产品/规格层面的判断，或你没法验证（reason 写清楚卡在哪），会转给协调人。
- 每个任务都要给结论；reason 一两句中文，具体到文件/函数。

项目验收规矩（摘自 AGENTS.md）：
{rules}

===== 本批任务材料 =====
{packets}
"""


def now() -> float:
    return time.time()


def queue_path(root: Path) -> Path:
    return bus_dir(root) / "review_queue.json"


@contextlib.contextmanager
def queue_lock(root: Path):
    path = bus_dir(root) / "review_queue.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def load_queue(root: Path) -> list[dict]:
    path = queue_path(root)
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    except (OSError, ValueError):
        return []


def save_queue(root: Path, items: list[dict]) -> None:
    path = queue_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def enqueue(root: Path, plan: str, task: str, agent: str) -> None:
    """同一 计划/任务 只排一份（重新 done 刷新时间与车道 agent）。"""
    with queue_lock(root):
        items = [i for i in load_queue(root) if (i["plan"], i["task"]) != (plan, task)]
        items.append({"plan": plan, "task": task, "agent": agent, "ts": now(), "attempts": 0})
        save_queue(root, items)


def running_pid(root: Path) -> int | None:
    path = bus_dir(root) / "review.pid"
    try:
        pid = int(path.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def due(root: Path) -> bool:
    items = load_queue(root)
    if not items or running_pid(root):
        return False
    return len(items) >= BATCH or now() - min(i["ts"] for i in items) >= MAX_WAIT


def spawn(root: Path) -> None:
    subprocess.Popen([sys.executable, str(TOOLS / "review_runner.py"), "--root", str(root), "run"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def acceptance_rules(repo: Path) -> str:
    """AGENTS.md 里与验收相关的两节原文。"""
    path = repo / "AGENTS.md"
    if not path.is_file():
        return "（未找到 AGENTS.md）"
    text = path.read_text(encoding="utf-8")
    keep = []
    for block in re.split(r"(?m)^(?=## )", text):
        if block.startswith(("## 验收铁律", "## 测试与 verify 门槛", "## 产品边界")):
            keep.append(block.strip())
    return "\n\n".join(keep) or "（AGENTS.md 无验收章节）"


def packet(root: Path, plan: str, task: str) -> str:
    out = subprocess.run([sys.executable, str(TOOLS / "task_state.py"), "--root", str(root), "--plan", plan,
                          "packet", task, "--max-lines", "300"], capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else f"# {task}：材料生成失败\n{out.stderr[-500:]}"


def ask_claude(repo: Path, prompt: str) -> tuple[dict | None, dict]:
    cmd = [claude_bin(), "-p", prompt, "--model", MODEL, "--effort", EFFORT, "--output-format", "json",
           "--json-schema", json.dumps(SCHEMA), "--no-session-persistence",
           "--allowedTools", "Read", "Grep", "Glob", "Bash",
           "--disallowedTools", "Edit", "Write", "NotebookEdit",
           "--max-budget-usd", str(BUDGET_USD)]
    try:
        proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=RUN_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, {"error": str(exc)[:300]}
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None, {"error": f"exit {proc.returncode}: {(proc.stderr or proc.stdout)[-300:]}"}
    meta = {k: data.get(k) for k in ("total_cost_usd", "num_turns", "duration_ms", "subtype", "is_error")}
    meta["usage"] = data.get("usage")
    if data.get("is_error") or not isinstance(data.get("structured_output"), dict):
        meta["error"] = str(data.get("result"))[:300]
        return None, meta
    return data["structured_output"], meta


TEST_BLOCK = """- 需要跑测试时，在临时 worktree 里跑：
    wt=$(mktemp -d)/wt && git worktree add --detach "$wt" <提交SHA> && cd "$wt" &&
    {command}
  用完 `git worktree remove --force "$wt"`。只在 diff 涉及逻辑、且备注里的测试证据不足以判断时才跑；纯数据/文档/表现微调看 diff 即可。
"""
NO_TEST_BLOCK = "- 本项目未配置测试命令（.agentic/config.json review.test_command）：只看 diff 与备注里的证据，不自行跑测试。\n"


def reviewer(root: Path) -> str:
    return project_config.load(root)["reviewer_id"]


def build_prompt(root: Path, repo: Path, packets: str) -> str:
    cfg = project_config.load(root)
    cmd = cfg["review"].get("test_command", "").strip()
    return INSTRUCTIONS.format(project=cfg["display_name"], reviewer=cfg["reviewer_id"], levels=cfg["review"]["levels"],
                               test_block=TEST_BLOCK.format(command=cmd) if cmd else NO_TEST_BLOCK,
                               rules=acceptance_rules(repo), packets=packets)


def bus_send(root: Path, to: str, mtype: str, task: str, body: str, plan: str) -> None:
    subprocess.run([sys.executable, str(TOOLS / "bus.py"), "--root", str(root), "send", "--from", reviewer(root),
                    "--to", to, "--type", mtype, "--task", task, "--plan", plan, "--body", body],
                   capture_output=True, text=True)


def coordinators(root: Path, plan: str) -> list[str]:
    import plans as plans_mod
    doc = plans_mod.plan_dir(root, plan) / plans_mod.DOC_NAME
    return sorted(set(plans_mod.COORD_RE.findall(doc.read_text(encoding="utf-8")))) if doc.is_file() else []


def apply(root: Path, item: dict, result: dict) -> str:
    verdict, plan, task = result["verdict"], item["plan"], item["task"]
    reason = result.get("reason", "").strip()
    fix = result.get("fix", "").strip()
    tests = result.get("tests_run", "").strip()
    note = reason + (f"；需修：{fix}" if fix and verdict == "reject" else "") + (f"；测试：{tests}" if tests else "")
    proc = subprocess.run([sys.executable, str(TOOLS / "task_state.py"), "--root", str(root), "--plan", plan,
                           "review", task, "--agent", reviewer(root), "--verdict", verdict, "--note", note],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return f"skip（{proc.stderr.strip()[:80]}）"  # 车道已自己 revoke 等：结论作废
    if verdict == "pass":
        bus_send(root, item["agent"], "ACCEPT", task, f"验收通过：{note}", plan)
    elif verdict == "reject":
        bus_send(root, item["agent"], "REJECT", task,
                 f"验收打回（{task} 已退回 active）：{reason}。需修：{fix or '见理由'}。修好后照常 done，会重新验收。", plan)
    else:
        for coord in coordinators(root, plan):
            bus_send(root, coord, "REQUEST_HELP", task, f"验收存疑，需协调人判断：{note}", plan)
    return verdict


def log_batch(root: Path, record: dict) -> None:
    path = bus_dir(root) / "metrics" / "reviews.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def run(root: Path, force: bool = False) -> int:
    pid_path = bus_dir(root) / "review.pid"
    if running_pid(root):
        print("已有验收在跑")
        return 0
    with queue_lock(root):
        items = load_queue(root)
        if not items or (not force and not due(root)):
            print("队列未到验收条件")
            return 0
        batch, rest = items[:MAX_BATCH], items[MAX_BATCH:]
        save_queue(root, rest)
        pid_path.write_text(str(os.getpid()))
    started = now()
    repo = root.resolve().parent
    try:
        prompt = build_prompt(root, repo, "\n\n".join(packet(root, i["plan"], i["task"]) for i in batch))
        output, meta = ask_claude(repo, prompt)
        outcomes = {}
        if output is None:
            with queue_lock(root):
                queue = load_queue(root)
                for item in batch:
                    item["attempts"] = item.get("attempts", 0) + 1
                    if item["attempts"] >= MAX_ATTEMPTS:
                        for coord in coordinators(root, item["plan"]):
                            bus_send(root, coord, "REQUEST_HELP", item["task"],
                                     f"验收 agent 连续 {item['attempts']} 次没能完成验收：{meta.get('error', '')}",
                                     item["plan"])
                        outcomes[item["task"]] = "gave_up"
                    else:
                        queue.insert(0, item)
                        outcomes[item["task"]] = "requeued"
                save_queue(root, queue)
        else:
            by_task = {r.get("task"): r for r in output.get("results", [])}
            for item in batch:
                result = by_task.get(item["task"]) or {"verdict": "unsure", "reason": "验收 agent 没有给出这个任务的结论"}
                outcomes[item["task"]] = apply(root, item, result)
        log_batch(root, {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         "tasks": [f"{i['plan']}/{i['task']}" for i in batch], "outcomes": outcomes,
                         "seconds": round(now() - started), "model": MODEL, "effort": EFFORT, **meta})
        print(json.dumps(outcomes, ensure_ascii=False))
        return 0
    finally:
        with contextlib.suppress(OSError):
            pid_path.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验收 agent：攒批后用全新 Claude Sonnet 会话验收 done 任务。")
    parser.add_argument("--root", type=Path, default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("run")
    p.add_argument("--force", action="store_true", help="不管是否攒够立即验收")
    p = sub.add_parser("enqueue")
    p.add_argument("--plan", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--agent", required=True)
    p = sub.add_parser("trial")
    p.add_argument("--plan", required=True)
    p.add_argument("--task", required=True, help="逗号分隔")
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else default_root()
    if args.cmd == "trial":
        repo = root.resolve().parent
        tasks = [t.strip() for t in args.task.split(",") if t.strip()]
        started = now()
        prompt = build_prompt(root, repo, "\n\n".join(packet(root, args.plan, t) for t in tasks))
        output, meta = ask_claude(repo, prompt)
        print(json.dumps({"seconds": round(now() - started), "prompt_chars": len(prompt), **meta,
                          "output": output}, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "enqueue":
        enqueue(root, args.plan, args.task, args.agent)
        return 0
    if args.cmd == "run":
        return run(root, args.force)
    items = load_queue(root)
    print(f"验收队列 {len(items)} 条；{'验收进行中 pid ' + str(running_pid(root)) if running_pid(root) else '空闲'}；"
          f"{'已到验收条件' if due(root) else '未到验收条件'}（{BATCH} 条或最早等满 {MAX_WAIT // 60} 分钟）")
    for i in items:
        print(f"  {i['plan']}/{i['task']}  {i['agent']}  等了 {int((now() - i['ts']) / 60)} 分  失败 {i.get('attempts', 0)} 次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
