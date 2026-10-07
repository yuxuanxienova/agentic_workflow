#!/usr/bin/env python3
"""开 Claude 车道会话：VS Code 新标签 + 会话 socket 投递首条提示（2026-10-07 plan7 实测唯一可行做法）。

流程（每条车道串行一次）：
  1. `code -r <仓库>` 把目标工作区窗口置前（URI 只发给最后聚焦的窗口）；
  2. `code --open-url vscode://anthropic.claude-code/open`（不带 session/prompt）——
     扩展立刻起一个 entrypoint=claude-vscode 的会话进程，带 messaging socket；
  3. 在 ~/.claude/sessions/ 找到这个新进程，用 bus_wake.post_claude 把完整车道提示写进 socket
     （等同用户在输入框回车，需用户级 crossSessionInbound=accept）；
  4. 首轮写出对话记录后追加 custom-title = <计划>-<车道>，侧栏即显示车道名。

不要用：`claude -p` / `claude --bg` 起车道（扩展 2.1.290 起侧栏过滤 sdk-* entrypoint 与 bg/daemon 会话，
`open?session=` 也静默失败）；不要把中文提示塞进 URL 的 prompt 参数（只预填不发送，且 bash 子 shell 编码会坏）。
详见 doc/setup_claude.html §5b。

用法：
  python3 tools/launch_claude_lanes.py --plan plan7 --lanes DATA,CONTENT [--note DATA="接手未提交的 …"]
  车道 → agent ID 从 .agentic/plans/<计划>/plan.md 的车道表读取；只开 claude-* 车道。
"""
from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bus_wake  # noqa: E402

CODE = "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"
SESSIONS = Path.home() / ".claude" / "sessions"


def repo_root() -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True)
    return Path(out.stdout.strip())


def project_dir(root: Path) -> Path:
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(root))


def lane_agents(root: Path, plan: str) -> dict:
    text = (root / ".agentic" / "plans" / plan / "plan.md").read_text(encoding="utf-8")
    lanes = {}
    for m in re.finditer(r"^\|\s*([A-Z0-9_]+)\s*\|\s*([a-z][\w./-]*)\s*\|\s*`", text, re.M):
        lanes[m.group(1)] = m.group(2)
    return lanes


def lane_prompt(plan: str, lane: str, agent: str, coord: str, note: str) -> str:
    return (
        f"你是 {plan} 的 {lane} 车道 agent，身份 ID 固定为 {agent}（task_state.py / bus.py 一律 --agent/--from {agent}，并带 --plan {plan}）。"
        f"协调人 {coord}。先读 AGENTS.md 与 .agentic/plans/{plan}/plan.md（车道表 {lane} 行是你的任务顺序），"
        f"再 bus.py read --agent {agent} --unread 和 task_state.py --plan {plan} status 看自己已认领/进行中的任务（同 ID 旧会话已停的话直接接手续做）。{note}"
        "然后逐个 claim → 做 → 验收 → 只提交自己的改动（私有 GIT_INDEX_FILE + git add 具体文件）→ done（备注写等级、证据路径、本任务提交 SHA）。"
        f"前置未 done 时 bus_wake.py until --agent {agent} --task <前置> 后结束本轮。共享工作树禁 stash/reset/全树 checkout；"
        "验收截图在临时 worktree 做。整条车道做完再停，不要中途问；问题走总线找协调人。"
    )


def live_vscode_sessions() -> dict:
    out = {}
    for f in SESSIONS.glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if d.get("entrypoint") == "claude-vscode" and d.get("messagingSocketPath"):
            out[d["sessionId"]] = d
    return out


def open_tab(root: Path, before: set, timeout: float = 40) -> dict | None:
    subprocess.run([CODE, "-r", str(root)], check=False)
    time.sleep(1)
    subprocess.run([CODE, "--open-url", "vscode://anthropic.claude-code/open"], check=False)
    deadline = time.time() + timeout
    while time.time() < deadline:
        new = [d for sid, d in live_vscode_sessions().items() if sid not in before]
        if new:
            return new[0]
        time.sleep(1)
    return None


def set_title(root: Path, sid: str, title: str, timeout: float = 120) -> bool:
    path = project_dir(root) / f"{sid}.jsonl"
    deadline = time.time() + timeout
    while time.time() < deadline and not path.exists():
        time.sleep(2)
    if not path.exists():
        return False
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "custom-title", "customTitle": title, "sessionId": sid}, ensure_ascii=False) + "\n")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--plan", required=True)
    ap.add_argument("--lanes", required=True, help="逗号分隔的车道名")
    ap.add_argument("--coord", default=None, help="协调人 ID（默认取车道表上方的 claude-*-coord，或 claude-<项目>-<计划>coord）")
    ap.add_argument("--note", action="append", default=[], help="LANE=附加说明，可多次")
    args = ap.parse_args()

    if os.environ.get("LANG", "").upper().find("UTF-8") < 0:
        os.environ["LANG"] = "en_US.UTF-8"
    root = repo_root()
    agents = lane_agents(root, args.plan)
    notes = dict(n.split("=", 1) for n in args.note)
    coord = args.coord or f"claude-{root.name.lower()}-{args.plan.replace('plan', 'p')}coord"

    rc = 0
    for lane in [x.strip() for x in args.lanes.split(",") if x.strip()]:
        agent = agents.get(lane)
        if not agent or not agent.startswith("claude-"):
            print(f"{lane}: 车道表里没有 claude-* agent（{agent}），跳过", file=sys.stderr)
            rc = 1
            continue
        before = set(live_vscode_sessions())
        d = open_tab(root, before)
        if not d:
            print(f"{lane}: 等不到新的 claude-vscode 会话（VS Code 窗口没聚焦到 {root}？）", file=sys.stderr)
            rc = 1
            continue
        bus_wake.post_claude(d["messagingSocketPath"], lane_prompt(args.plan, lane, agent, coord, notes.get(lane, "")))
        titled = set_title(root, d["sessionId"], f"{args.plan}-{lane}")
        print(f"{lane}\t{agent}\tpid {d['pid']}\t{d['sessionId']}\t{'titled' if titled else 'title pending'}")
    print("核对：python3 agentic_workflow/tools/bus_wake.py list（每个车道 ID 应对应不同 pid）")
    return rc


if __name__ == "__main__":
    sys.exit(main())
