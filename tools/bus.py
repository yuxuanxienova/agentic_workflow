#!/usr/bin/env python3
"""消息总线 CLI·agent 间通讯（通用 Agentic Workflow 版）。

两个 append-only 文件：
  <root>/bus/log.jsonl              全局权威消息流（所有消息都落这里；全局一份，不按计划分）
  <root>/bus/inbox/<agent_id>.jsonl 定向收件箱（仅点对点消息落这里）

消息结构：
  {"seq", "ts", "from", "to", "type", "task_id", "body", "refs"[, "plan"]}

合法类型：CLAIM / RELEASE / CONFLICT / PROPOSE / ACCEPT / REJECT /
         REQUEST_HELP / INTERRUPT / HANDOFF / BLOCKED / DONE（不合法退出 2）。

子命令：
  send --from <id> --to <id> --type <TYPE> [--task <id>] [--body <文本>] [--refs a,b]
       同时写入 to 的收件箱与 log.jsonl；to=* 表示广播（只写 log.jsonl）。--plan 可选标注所属计划。
  read --agent <id> [--since <seq>]   读取某 agent 的收件箱 + 他人广播（默认全部）。
  read --agent <id> --unread          只读游标之后的新消息并推进游标（跨厂商轮询用）。
  tail [--n N]                        读 log.jsonl 尾部 N 条（默认 20）。

append-only 文件用单次 O_APPEND 写入保证原子性（不以 os.replace 重写，避免破坏追加语义）。

agent ID 须为 <厂商>-<会话/实例>[/<子agent>]（与 task_state.py 同规则），广播用 *。
收件箱是正式通讯渠道：任何厂商的 agent 都能读写；厂商自带的会话消息（如 Claude Code
SendMessage）只用于「叫醒对方去读收件箱」，不承载需要留痕的内容。

默认 <root> = <git 仓库根>/.agentic。

退出码：0 成功；2 用法或数据错误（如非法的消息类型）。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BUS_DIR = "bus"
EVENTS_NAME = "log.jsonl"
INBOX_DIR = "inbox"
DEFAULT_ROOT = ".agentic"
BROADCAST = "*"
LOCK_NAME = ".bus.lock"
AGENT_ID_RE = re.compile(r"^[a-z]+-[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$")
MESSAGE_TYPES = (
    "CLAIM",
    "RELEASE",
    "CONFLICT",
    "PROPOSE",
    "ACCEPT",
    "REJECT",
    "REQUEST_HELP",
    "INTERRUPT",
    "HANDOFF",
    "BLOCKED",
    "DONE",
)


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


def append_jsonl(path: Path, payload: dict) -> None:
    """单次写入追加一行 JSON（O_APPEND，append-only 文件保持原子追加语义）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line)


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


def bus_dir(root: Path) -> Path:
    """总线全局一份：<root>/bus/（不随计划分开——一个 agent 可能同时参与多个计划）。"""
    return root / BUS_DIR


def log_path(root: Path) -> Path:
    return bus_dir(root) / EVENTS_NAME


def next_seq(root: Path) -> int:
    """扫描 bus/log.jsonl 取最大 seq，返回下一个可用序号。"""
    last = 0
    for entry in iter_jsonl(log_path(root)):
        seq = entry.get("seq")
        if isinstance(seq, int) and seq > last:
            last = seq
    return last + 1


@contextlib.contextmanager
def bus_lock(root: Path):
    """取 seq 与追加在同一把排他锁内，避免多会话撞号。"""
    bus_dir(root).mkdir(parents=True, exist_ok=True)
    with open(bus_dir(root) / LOCK_NAME, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def inbox_path(root: Path, agent: str) -> Path:
    """子 agent ID 含 /，落盘时换成 __，避免变成子目录。"""
    return bus_dir(root) / INBOX_DIR / f"{agent.replace('/', '__')}.jsonl"


def cursor_path(root: Path, agent: str) -> Path:
    return bus_dir(root) / INBOX_DIR / f"{agent.replace('/', '__')}.cursor"


def bad_agent_id(agent: str) -> bool:
    return not AGENT_ID_RE.match(agent or "")


def now_iso() -> str:
    """当前时间的 ISO 8601（UTC，秒级）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_msg(entry: dict) -> str:
    """把一条消息格式化为单行可读文本。"""
    refs = ",".join(entry.get("refs") or [])
    refs_part = f" refs=[{refs}]" if refs else ""
    plan_part = f"[{entry['plan']}] " if entry.get("plan") else ""
    return (
        f"{plan_part}#{entry.get('seq'):<4} {entry.get('ts', '-'):<20} "
        f"{entry.get('from', '-'):<12}→{entry.get('to', '-'):<12} "
        f"[{entry.get('type', '-'):<12}] task={entry.get('task_id') or '-':<10} "
        f"{entry.get('body') or ''}{refs_part}"
    ).rstrip()


def cmd_send(args) -> int:
    """发送一条消息。"""
    message_type = args.type.upper()
    if message_type not in MESSAGE_TYPES:
        print(f"非法消息类型：{args.type}（合法值：{'/'.join(MESSAGE_TYPES)}）", file=sys.stderr)
        return 2
    for role, agent in (("--from", args.sender), ("--to", args.recipient)):
        if agent == BROADCAST and role == "--to":
            continue
        if bad_agent_id(agent):
            print(f"非法 agent ID {role}「{agent}」：须为 <厂商>-<会话/实例>[/<子agent>]，如 claude-endlessspace-d5", file=sys.stderr)
            return 2
    body = args.body or ""
    if body.lstrip().startswith("ART:") and "ref:" not in body:
        print("ART: 请求必须带 ref:<参考图路径>（1–3 张，作为生图的图像输入；见 AGENTS.md）", file=sys.stderr)
        return 2
    refs = [item.strip() for item in (args.refs or "").split(",") if item.strip()]
    with bus_lock(args.root):
        entry = {
            "seq": next_seq(args.root),
            "ts": now_iso(),
            "from": args.sender,
            "to": args.recipient,
            "type": message_type,
            "task_id": args.task or None,
            "body": args.body or "",
            "refs": refs,
        }
        plan = getattr(args, "plan", None) or infer_plan(args.root, args.task)
        if plan:
            entry["plan"] = plan
        append_jsonl(log_path(args.root), entry)
        if args.recipient != BROADCAST:
            append_jsonl(inbox_path(args.root, args.recipient), entry)
    if args.recipient != BROADCAST:
        print(f"✓ 已发送 #{entry['seq']} {message_type} {args.sender}→{args.recipient}（投递收件箱 + 事件流）")
    else:
        print(f"✓ 已广播 #{entry['seq']} {message_type} {args.sender}→*（总日志；所有人 read 时可见）")
    wake_recipients(args.root, args.recipient, args.sender, message_type)
    return 0


def wake_recipients(root: Path, recipient: str, sender: str, message_type: str = "") -> None:
    """收件人若登记在 bus_wake 映射里（Codex daemon 会话），后台等它空闲后叫醒；失败不影响发送。"""
    if not (bus_dir(root) / "wake_map.json").is_file():
        return
    try:
        from bus_wake import spawn_await
        spawn_await(root, recipient, sender, message_type)
    except Exception as exc:  # noqa: BLE001  唤醒是增强，不能让 send 失败
        print(f"（唤醒钩子未生效：{exc}）", file=sys.stderr)


def infer_plan(root: Path, task: str | None) -> str | None:
    """没给 --plan 时按 --task 在 active 计划里唯一匹配补 plan 标签；推断不出就不打。"""
    if not task:
        return None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import plans as plans_mod
        from task_state import plan_task_ids
        return plans_mod.task_plan(root, task, plan_task_ids)
    except Exception:  # noqa: BLE001  标签是增强，不能让 send 失败
        return None


def cmd_read(args) -> int:
    """读取某个 agent 的收件箱。"""
    since = args.since if args.since is not None else 0
    cursor = cursor_path(args.root, args.agent)
    if args.unread and cursor.is_file():
        since = max(since, int(cursor.read_text(encoding="utf-8").strip() or 0))
    # 收件箱（点对点）+ 别人发的广播（只落总日志），按 seq 合并
    messages = [entry for entry in iter_jsonl(inbox_path(args.root, args.agent)) if entry.get("seq", 0) > since]
    messages += [entry for entry in iter_jsonl(log_path(args.root))
                 if entry.get("to") == BROADCAST and entry.get("from") != args.agent and entry.get("seq", 0) > since]
    messages.sort(key=lambda m: m.get("seq", 0))
    if args.unread and messages:
        cursor.write_text(str(max(int(m.get("seq", 0)) for m in messages)), encoding="utf-8")
    if args.json:
        print(json.dumps(messages, ensure_ascii=False, indent=2))
        return 0
    if not messages:
        print(f"（{args.agent} 无新消息）")
        return 0
    for entry in messages:
        print(fmt_msg(entry))
    return 0


def cmd_tail(args) -> int:
    """读取全局事件流尾部。"""
    events = list(iter_jsonl(log_path(args.root)))
    tail = events[-args.n :] if args.n > 0 else []
    if args.json:
        print(json.dumps(tail, ensure_ascii=False, indent=2))
        return 0
    if not tail:
        print("（事件流为空）")
        return 0
    for entry in tail:
        print(fmt_msg(entry))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 解析器。"""
    parser = argparse.ArgumentParser(description="消息总线：agent 间的 CLAIM/CONFLICT/HANDOFF 等事件通讯。")
    parser.add_argument("--root", type=Path, default=None, help="协调目录（默认 <git 根>/.agentic）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_send = sub.add_parser("send", help="发送一条消息")
    p_send.add_argument("--from", dest="sender", required=True, help="发送方 agent ID")
    p_send.add_argument("--to", dest="recipient", required=True, help="接收方 agent ID，* 表示广播")
    p_send.add_argument("--type", dest="type", required=True, help=f"消息类型：{'/'.join(MESSAGE_TYPES)}")
    p_send.add_argument("--task", default=None, help="关联任务 ID（可选）")
    p_send.add_argument("--body", default=None, help="消息正文（可选）")
    p_send.add_argument("--refs", default=None, help="引用，逗号分隔（可选）")
    p_send.add_argument("--plan", default=None, help="关联计划名（可选，便于按计划过滤）")
    p_send.set_defaults(func=cmd_send)

    p_read = sub.add_parser("read", help="读取某 agent 的收件箱")
    p_read.add_argument("--agent", required=True, help="收件方 agent ID")
    p_read.add_argument("--since", type=int, default=None, help="只读 seq 大于该值的消息")
    p_read.add_argument("--unread", action="store_true", help="只读游标之后的新消息并推进游标")
    p_read.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_read.set_defaults(func=cmd_read)

    p_tail = sub.add_parser("tail", help="读全局事件流尾部")
    p_tail.add_argument("--n", type=int, default=20, help="读取条数（默认 20）")
    p_tail.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_tail.set_defaults(func=cmd_tail)

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
