#!/usr/bin/env python3
"""文件总线·Agent 注册表 CLI（通用 Agentic Workflow 版）。

以文件为共享总线，让多个厂商的 AI agent 在同一仓库协作时能互相发现：
谁在干什么、占了哪些文件、是否还活着。存储为 <root>/registry.json，结构：

  {
    "version": 1,
    "seq": <int>,
    "agents": {
      "<agent_id>": {
        "agent_id", "model", "role", "task_id", "files": [...],
        "branch", "worktree", "status", "heartbeat_ts", "registered_ts"
      }
    }
  }

子命令：
  register  --agent --model --role --task --files a,b,c --branch [--worktree]
            注册或更新一个 agent（同一 agent 重复 register 视为更新）。
  heartbeat --agent                        刷新心跳时间。
  release   --agent                        从注册表移除（收工）。
  list                                     列出所有已注册 agent。
  who-owns  <path>                         列出当前持有该路径的 agent（路径重叠判定）。
  stale     --ttl <秒>                      列出手心跳超时的 agent。

所有写操作（register/heartbeat/release）都会让 registry.json 的 seq 自增 1。
默认 <root> = <git 仓库根>/.agentic。

退出码：0 成功；2 用法或数据错误（如 agent 不存在）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_file_ownership import specs_overlap  # noqa: E402  复用同一份路径重叠判定

REGISTRY_NAME = "registry.json"
DEFAULT_ROOT = ".agentic"


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


def atomic_write_json(path: Path, payload: dict) -> None:
    """原子写入 JSON：先写同目录临时文件再 os.replace。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_registry(root: Path) -> dict:
    """读取注册表；不存在时返回空结构。"""
    path = root / REGISTRY_NAME
    if not path.is_file():
        return {"version": 1, "seq": 0, "agents": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("version", 1)
    data.setdefault("seq", 0)
    data.setdefault("agents", {})
    return data


def save_registry(root: Path, data: dict) -> None:
    """写回注册表。"""
    atomic_write_json(root / REGISTRY_NAME, data)


def fmt_ts(ts: float | int | None) -> str:
    """把 epoch 秒格式化为可读时间。"""
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def cmd_register(args) -> int:
    """注册或更新一个 agent。"""
    data = load_registry(args.root)
    now = int(time.time())
    files = [item.strip() for item in args.files.split(",") if item.strip()]
    existing = data["agents"].get(args.agent, {})
    entry = {
        "agent_id": args.agent,
        "model": args.model,
        "role": args.role,
        "task_id": args.task,
        "files": files,
        "branch": args.branch,
        "worktree": args.worktree or "",
        "status": "active",
        "heartbeat_ts": now,
        "registered_ts": existing.get("registered_ts", now),
    }
    data["agents"][args.agent] = entry
    data["seq"] += 1
    save_registry(args.root, data)
    action = "更新" if existing else "注册"
    print(f"✓ 已{action} agent {args.agent}（task={args.task}，{len(files)} 个文件，seq={data['seq']}）")
    return 0


def cmd_heartbeat(args) -> int:
    """刷新某个 agent 的心跳。"""
    data = load_registry(args.root)
    entry = data["agents"].get(args.agent)
    if entry is None:
        print(f"未知 agent：{args.agent}", file=sys.stderr)
        return 2
    entry["heartbeat_ts"] = int(time.time())
    data["seq"] += 1
    save_registry(args.root, data)
    print(f"✓ agent {args.agent} 心跳已刷新（seq={data['seq']}）")
    return 0


def cmd_release(args) -> int:
    """从注册表移除一个 agent。"""
    data = load_registry(args.root)
    if args.agent not in data["agents"]:
        print(f"未知 agent：{args.agent}", file=sys.stderr)
        return 2
    del data["agents"][args.agent]
    data["seq"] += 1
    save_registry(args.root, data)
    print(f"✓ 已释放 agent {args.agent}（seq={data['seq']}）")
    return 0


def cmd_list(args) -> int:
    """列出所有已注册 agent。"""
    data = load_registry(args.root)
    agents = data["agents"]
    if args.json:
        print(json.dumps(agents, ensure_ascii=False, indent=2))
        return 0
    if not agents:
        print("（注册表为空）")
        return 0
    print(f"{'AGENT':<16}{'MODEL':<12}{'ROLE':<14}{'TASK':<12}{'状态':<8}心跳")
    for agent_id in sorted(agents):
        entry = agents[agent_id]
        print(
            f"{agent_id:<16}{entry.get('model', '-'):<12}{entry.get('role', '-'):<14}"
            f"{entry.get('task_id', '-'):<12}{entry.get('status', '-'):<8}{fmt_ts(entry.get('heartbeat_ts'))}"
        )
    return 0


def cmd_who_owns(args) -> int:
    """列出当前持有目标路径（含重叠）的 agent。"""
    data = load_registry(args.root)
    holders = []
    for agent_id in sorted(data["agents"]):
        entry = data["agents"][agent_id]
        for spec in entry.get("files", []):
            if specs_overlap(spec, args.path):
                holders.append(
                    {
                        "agent_id": agent_id,
                        "task_id": entry.get("task_id"),
                        "spec": spec,
                        "self": spec == args.path,
                    }
                )
                break
    if args.json:
        print(json.dumps(holders, ensure_ascii=False, indent=2))
        return 0
    if not holders:
        print(f"路径未被持有：{args.path}")
        return 0
    print(f"持有 {args.path} 的 agent：")
    for holder in holders:
        mark = "（精确）" if holder["self"] else f"（重叠 {holder['spec']}）"
        print(f"  {holder['agent_id']}  task={holder['task_id']} {mark}")
    return 0


def cmd_stale(args) -> int:
    """列出手心跳超时的 agent。"""
    data = load_registry(args.root)
    now = time.time()
    stale = []
    for agent_id in sorted(data["agents"]):
        entry = data["agents"][agent_id]
        last = entry.get("heartbeat_ts") or 0
        age = now - last
        if age > args.ttl:
            stale.append({"agent_id": agent_id, "task_id": entry.get("task_id"), "age_seconds": int(age)})
    if args.json:
        print(json.dumps(stale, ensure_ascii=False, indent=2))
        return 0
    if not stale:
        print(f"（无心跳超时 agent，TTL={args.ttl}s）")
        return 0
    print(f"心跳超时 agent（TTL={args.ttl}s）：")
    for item in stale:
        print(f"  {item['agent_id']}  task={item['task_id']}  已 {item['age_seconds']}s 无心跳")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 解析器。"""
    parser = argparse.ArgumentParser(description="文件总线·Agent 注册表：谁在干什么、占了哪些文件、是否还活着。")
    parser.add_argument("--root", type=Path, default=None, help="协调目录（默认 <git 根>/.agentic）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_reg = sub.add_parser("register", help="注册或更新一个 agent")
    p_reg.add_argument("--agent", required=True, help="agent 唯一 ID")
    p_reg.add_argument("--model", required=True, help="模型名，如 claude/gpt/codex")
    p_reg.add_argument("--role", required=True, help="角色，如 implementer/reviewer")
    p_reg.add_argument("--task", required=True, help="任务 ID")
    p_reg.add_argument("--files", required=True, help="改动的文件，逗号分隔")
    p_reg.add_argument("--branch", required=True, help="分支名")
    p_reg.add_argument("--worktree", default=None, help="worktree 路径（可选）")
    p_reg.set_defaults(func=cmd_register)

    p_hb = sub.add_parser("heartbeat", help="刷新心跳")
    p_hb.add_argument("--agent", required=True, help="agent 唯一 ID")
    p_hb.set_defaults(func=cmd_heartbeat)

    p_rel = sub.add_parser("release", help="从注册表移除 agent")
    p_rel.add_argument("--agent", required=True, help="agent 唯一 ID")
    p_rel.set_defaults(func=cmd_release)

    p_list = sub.add_parser("list", help="列出所有已注册 agent")
    p_list.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_list.set_defaults(func=cmd_list)

    p_own = sub.add_parser("who-owns", help="列出持有某路径的 agent")
    p_own.add_argument("path", help="要查询的仓库相对路径")
    p_own.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_own.set_defaults(func=cmd_who_owns)

    p_stale = sub.add_parser("stale", help="列出手心跳超时的 agent")
    p_stale.add_argument("--ttl", type=float, required=True, help="心跳超时阈值（秒）")
    p_stale.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_stale.set_defaults(func=cmd_stale)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.root is None:
        args.root = default_root()
    args.root = Path(args.root)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
