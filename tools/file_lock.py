#!/usr/bin/env python3
"""CAS 文件锁 CLI（通用 Agentic Workflow 版）。

保证「同一文件同一时刻只有一个 agent 在改」。锁文件放在 <root>/locks/，
文件名 = sha1(仓库相对路径).hexdigest() + ".json"，内容：

  {"path", "owner", "task_id", "acquired_ts", "ttl", "seq"}

acquire 采用「读取—校验—原子替换（os.replace）」的 CAS 语义：若目标路径
已被他人持有且锁未过期，则退出 1 并打印持有者；同一 owner 重复 acquire 视为
续租，退出 0。TTL 默认 900 秒（15 分钟）。

子命令：
  acquire <path> --agent <id> --task <id> [--ttl <秒>]
  release <path> --agent <id>
  status  <path>
  list
  sweep            清理所有已过期锁。

默认 <root> = <git 仓库根>/.agentic。

退出码：0 成功；1 锁被他人持有 / 释放非自己持有的锁；2 用法或数据错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

LOCKS_DIR = "locks"
DEFAULT_TTL = 900
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


def lock_path(root: Path, target: str) -> Path:
    """由目标路径推导锁文件路径。"""
    digest = hashlib.sha1(target.encode("utf-8")).hexdigest()
    return root / LOCKS_DIR / f"{digest}.json"


def read_lock(path: Path) -> dict | None:
    """读取锁文件；不存在返回 None。"""
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def is_expired(lock: dict, now: float) -> bool:
    """判断锁是否已过期。"""
    return now >= lock.get("acquired_ts", 0) + lock.get("ttl", 0)


def fmt_ts(ts: float | int | None) -> str:
    """把 epoch 秒格式化为可读时间。"""
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def cmd_acquire(args) -> int:
    """获取或续租一个文件锁。"""
    path = lock_path(args.root, args.path)
    now = time.time()
    lock = read_lock(path)
    if lock is not None and not is_expired(lock, now) and lock.get("owner") != args.agent:
        print(
            f"✗ 锁被占用：{args.path} 当前由 {lock.get('owner')} "
            f"（task={lock.get('task_id')}，acquired={fmt_ts(lock.get('acquired_ts'))}，"
            f"ttl={lock.get('ttl')}s）持有",
            file=sys.stderr,
        )
        return 1
    seq = (lock.get("seq", 0) + 1) if lock is not None else 1
    payload = {
        "path": args.path,
        "owner": args.agent,
        "task_id": args.task,
        "acquired_ts": now,
        "ttl": args.ttl,
        "seq": seq,
    }
    atomic_write_json(path, payload)
    renewed = lock is not None and lock.get("owner") == args.agent
    print(f"✓ {'续租' if renewed else '获得'}锁：{args.path} → {args.agent}（ttl={args.ttl}s，seq={seq}）")
    return 0


def cmd_release(args) -> int:
    """释放一个文件锁（仅持有者）。"""
    path = lock_path(args.root, args.path)
    lock = read_lock(path)
    if lock is None:
        print(f"✗ 无法释放：{args.path} 当前未被加锁", file=sys.stderr)
        return 1
    if lock.get("owner") != args.agent:
        print(
            f"✗ 无法释放：{args.path} 由 {lock.get('owner')} 持有，你不是持有者",
            file=sys.stderr,
        )
        return 1
    os.unlink(path)
    print(f"✓ 已释放锁：{args.path}（原持有者 {args.agent}）")
    return 0


def cmd_status(args) -> int:
    """查看某个路径的锁状态。"""
    path = lock_path(args.root, args.path)
    lock = read_lock(path)
    if lock is None:
        print(f"{args.path}: 未加锁")
        return 0
    expired = is_expired(lock, time.time())
    state = "已过期" if expired else "有效"
    print(
        f"{args.path}: {state} → owner={lock.get('owner')} task={lock.get('task_id')} "
        f"acquired={fmt_ts(lock.get('acquired_ts'))} ttl={lock.get('ttl')}s seq={lock.get('seq')}"
    )
    return 0


def cmd_list(args) -> int:
    """列出所有锁。"""
    locks_dir = args.root / LOCKS_DIR
    if not locks_dir.is_dir():
        print("（无锁）")
        return 0
    now = time.time()
    rows = []
    for path in sorted(locks_dir.glob("*.json")):
        try:
            lock = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows.append((path, lock))
    if not rows:
        print("（无锁）")
        return 0
    print(f"{'PATH':<48}{'OWNER':<16}{'TASK':<10}{'状态':<8}获得时间")
    for path, lock in rows:
        state = "已过期" if is_expired(lock, now) else "有效"
        print(
            f"{lock.get('path', '-'):<48}{lock.get('owner', '-'):<16}"
            f"{lock.get('task_id', '-'):<10}{state:<8}{fmt_ts(lock.get('acquired_ts'))}"
        )
    return 0


def cmd_sweep(args) -> int:
    """清理所有已过期锁。"""
    locks_dir = args.root / LOCKS_DIR
    if not locks_dir.is_dir():
        print("（无锁可清理）")
        return 0
    now = time.time()
    removed = 0
    for path in sorted(locks_dir.glob("*.json")):
        try:
            lock = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            os.unlink(path)
            removed += 1
            continue
        if is_expired(lock, now):
            os.unlink(path)
            removed += 1
            print(f"  清理过期锁：{lock.get('path')}（owner={lock.get('owner')}）")
    print(f"✓ 已清理 {removed} 个过期锁")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 解析器。"""
    parser = argparse.ArgumentParser(description="CAS 文件锁：保证同一文件同一时刻只有一个 agent 在改。")
    parser.add_argument("--root", type=Path, default=None, help="协调目录（默认 <git 根>/.agentic）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_acq = sub.add_parser("acquire", help="获取/续租一个文件锁")
    p_acq.add_argument("path", help="要加锁的仓库相对路径")
    p_acq.add_argument("--agent", required=True, help="持有者 agent ID")
    p_acq.add_argument("--task", required=True, help="任务 ID")
    p_acq.add_argument("--ttl", type=float, default=DEFAULT_TTL, help=f"锁存活秒数（默认 {DEFAULT_TTL}）")
    p_acq.set_defaults(func=cmd_acquire)

    p_rel = sub.add_parser("release", help="释放一个文件锁")
    p_rel.add_argument("path", help="要解锁的仓库相对路径")
    p_rel.add_argument("--agent", required=True, help="持有者 agent ID")
    p_rel.set_defaults(func=cmd_release)

    p_st = sub.add_parser("status", help="查看某个路径的锁状态")
    p_st.add_argument("path", help="仓库相对路径")
    p_st.set_defaults(func=cmd_status)

    p_list = sub.add_parser("list", help="列出所有锁")
    p_list.set_defaults(func=cmd_list)

    p_sweep = sub.add_parser("sweep", help="清理所有已过期锁")
    p_sweep.set_defaults(func=cmd_sweep)

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
