#!/usr/bin/env python3
"""总线唤醒器·收件箱有新消息时叫醒 Codex / Claude Code 会话（通用 Agentic Workflow 版）。

Claude Code 会话：自动发现（~/.claude/sessions/*.json，agent ID 由对话记录推断），往其收件箱
socket 投递消息；需项目 .claude/settings.local.json 设 crossSessionInbound=accept。

原理：ChatGPT App 的 Codex「SSH 远程项目」（主机指向本机 codex-local）把会话放在本机共享
app-server daemon 里；本工具作为第二个客户端连同一个 daemon（WebSocket over unix socket），
对空闲会话 thread/resume + turn/start 注入一轮，App 里实时可见。App「本地项目」的会话由 App
私有 app-server 持写锁，叫不醒——必须用 codex-local 远程项目下的会话。完整流程与坑见
agentic_workflow/doc/setup_codex.html。

映射（本机私有，不入库）：<root>/bus/wake_map.json  {"<agent_id>": "<thread_id>"}
状态（本机私有，不入库）：<root>/bus/wake_state.json {"<agent_id>": <已唤醒到的 seq>}

子命令：
  map    --agent <id> --thread <thread_id|会话名>  登记 agent ↔ 会话
  unmap  --agent <id>                              取消登记
  list                                             列出映射与会话状态
  spawn  --agent <id> --name <会话名> --text <首轮提示> [--cwd 目录]
         在 daemon 新开会话（App 的 codex-local 项目下可见）、命名、发首轮，并登记映射
  wake   --agent <id> [--text <提示>] [--force]   叫醒一次（会话忙则跳过，退出 3）
  watch  [--interval 20] [--direct-only] [--once]
         轮询收件箱：某 agent 有游标之后的新消息且会话空闲 → 后台叫醒它读收件箱

退出码：0 成功；2 用法/连接错误；3 会话忙已跳过。
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bus import BROADCAST, bus_dir, cursor_path, default_root, inbox_path, iter_jsonl, log_path  # noqa: E402

SOCK = Path.home() / ".codex/app-server-control/app-server-control.sock"
MAP_NAME = "wake_map.json"
STATE_NAME = "wake_state.json"
DEFAULT_WAKE_TEXT = (
    "【bus_wake 自动唤醒】你的总线收件箱有新消息（#{seqs}）。请先跑 "
    "`python3 agentic_workflow/tools/bus.py read --agent {agent} --unread`，"
    "再按 AGENTS.md 的车道规矩处理：前置满足就继续车道，仍需等待就简短说明在等什么后结束本轮。"
)


class AppServer:
    """共享 app-server daemon 的最小 JSON-RPC 客户端（WebSocket over unix socket）。"""

    def __init__(self, sock_path: Path = SOCK):
        self.sock = socket.socket(socket.AF_UNIX)
        try:
            self.sock.connect(str(sock_path))
        except OSError as exc:
            raise SystemExit(f"连不上 daemon {sock_path}：{exc}（先跑 codex app-server daemon start）")
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            f"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            self.buf += self._recv_chunk()
        self.buf = self.buf.split(b"\r\n\r\n", 1)[1]
        self.next_id = 0
        self.call("initialize", {"clientInfo": {"name": "bus_wake", "version": "1"}})
        self.notify("initialized")

    def _recv_chunk(self) -> bytes:
        chunk = self.sock.recv(65536)
        if not chunk:
            raise SystemExit("daemon 关闭了连接")
        return chunk

    def _recv_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            self.buf += self._recv_chunk()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send_frame(self, data: bytes, opcode: int = 1) -> None:
        header = bytes([0x80 | opcode])
        n = len(data)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)  # 客户端帧必须加掩码
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self) -> dict:
        message = b""
        while True:
            b1, b2 = self._recv_exact(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(8))[0]
            payload = self._recv_exact(n)
            opcode = b1 & 0x0F
            if opcode == 9:
                self._send_frame(payload, 10)
                continue
            if opcode == 8:
                raise SystemExit("daemon 关闭了 WebSocket")
            message += payload
            if b1 & 0x80:
                return json.loads(message)

    def notify(self, method: str, params: dict | None = None) -> None:
        payload = {"method": method}
        if params is not None:
            payload["params"] = params
        self._send_frame(json.dumps(payload).encode())

    def call(self, method: str, params: dict) -> dict:
        self.next_id += 1
        request_id = self.next_id
        self._send_frame(json.dumps({"id": request_id, "method": method, "params": params}).encode())
        while True:
            message = self.recv()
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise RuntimeError(message["error"].get("message", message["error"]))
                return message.get("result", {})

    def follow_turn(self, quiet: bool = False) -> str:
        """打印 agent 消息与命令，直到 turn/completed，返回最终状态。"""
        while True:
            message = self.recv()
            method = message.get("method", "")
            if "id" in message and method:
                print(f"⚠ 会话发起了 {method}（审批/提问）——请到 App 里处理", flush=True)
                continue
            if method == "item/completed" and not quiet:
                item = message["params"].get("item", {})
                if item.get("type") == "agentMessage":
                    print(f"AGENT: {item.get('text', '')}", flush=True)
                elif item.get("type") == "commandExecution":
                    print(f"CMD: {item.get('command')}", flush=True)
            if method == "turn/completed":
                return message["params"].get("turn", {}).get("status", "?")


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def resolve_thread(value: str) -> str:
    """接受 thread UUID 或会话名（查 ~/.codex/session_index.jsonl，取最新同名）。"""
    if len(value) == 36 and value.count("-") == 4:
        return value
    found = None
    for entry in iter_jsonl(Path.home() / ".codex/session_index.jsonl"):
        if entry.get("thread_name") == value:
            found = entry.get("id")
    if not found:
        raise SystemExit(f"找不到名为「{value}」的会话")
    return found


def thread_status(server: AppServer, thread_id: str) -> str:
    """idle / active / notLoaded / systemError。"""
    return server.call("thread/read", {"threadId": thread_id})["thread"]["status"]["type"]


def pending_seqs(root: Path, agent: str, after: int, direct_only: bool) -> list[int]:
    """agent 游标之后、且大于 after 的消息 seq（只读，不推进 bus.py 的游标）。"""
    cursor = cursor_path(root, agent)
    since = int(cursor.read_text(encoding="utf-8").strip() or 0) if cursor.is_file() else 0
    since = max(since, after)
    seqs = [e.get("seq", 0) for e in iter_jsonl(inbox_path(root, agent)) if e.get("seq", 0) > since]
    if not direct_only:
        seqs += [e.get("seq", 0) for e in iter_jsonl(log_path(root))
                 if e.get("to") == BROADCAST and e.get("from") != agent and e.get("seq", 0) > since]
    return sorted(set(seqs))


def inject_turn(server: AppServer, thread_id: str, text: str) -> None:
    server.call("thread/resume", {"threadId": thread_id, "excludeTurns": True})
    server.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": text}]})


@contextlib.contextmanager
def state_lock(root: Path):
    """send 钩子 / watch / launchd 共用：判断 + 唤醒 + 记 seq 在同一把锁内，同一批消息只叫一次。"""
    bus_dir(root).mkdir(parents=True, exist_ok=True)
    with open(bus_dir(root) / ".wake.lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


CLAUDE_SESSIONS = Path.home() / ".claude/sessions"
CLAUDE_PROJECTS = Path.home() / ".claude/projects"
CLAUDE_AGENT_RE = re.compile(r"--(?:agent|from)[ =]+[\"']?(claude-[A-Za-z0-9._/-]+)")


def claude_sessions(repo: Path) -> dict[str, dict]:
    """本机在线、工作目录在本仓库的 Claude Code 会话 → {agent_id: 会话信息}。
    agent ID 取该会话对话记录里 --agent/--from 用得最多的 claude-* ID；同 ID 多个会话取最近活跃的。"""
    found: dict[str, dict] = {}
    for meta_path in CLAUDE_SESSIONS.glob("*.json"):
        meta = load_json(meta_path)
        pid, sock = meta.get("pid"), meta.get("messagingSocketPath")
        if not pid or not sock or not str(meta.get("cwd", "")).startswith(str(repo)):
            continue
        try:
            os.kill(int(pid), 0)
        except OSError:
            continue  # 进程已退出，元数据是残留
        transcripts = list(CLAUDE_PROJECTS.glob(f"*/{meta.get('sessionId')}.jsonl"))
        if not transcripts:
            continue
        counts: dict[str, int] = {}
        for agent in CLAUDE_AGENT_RE.findall(transcripts[0].read_text(encoding="utf-8", errors="ignore")):
            counts[agent] = counts.get(agent, 0) + 1
        if not counts:
            continue
        agent = max(counts, key=counts.get)
        meta["socket"] = sock
        if agent not in found or meta.get("updatedAt", 0) > found[agent].get("updatedAt", 0):
            found[agent] = meta
    return found


def post_claude(sock_path: str, text: str) -> None:
    """往 Claude Code 会话收件箱 socket 投递一条用户消息（空闲会话会自动开新一轮，忙则在工具调用间读到）。
    需要接收方 crossSessionInbound=accept（项目 .claude/settings.local.json），否则绕过权限的会话会挂起消息。"""
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(10)
        conn.connect(sock_path)
        conn.sendall((json.dumps({"type": "user", "message": {"role": "user", "content": text}},
                                 ensure_ascii=False) + "\n").encode())


def repo_root(root: Path) -> Path:
    return root.resolve().parent


def wake_targets(root: Path) -> dict[str, tuple[str, str]]:
    """{agent_id: ("codex", thread_id) | ("claude", socket)}；Codex 来自映射，Claude 自动发现。"""
    targets = {agent: ("codex", thread) for agent, thread in load_json(bus_dir(root) / MAP_NAME).items()}
    for agent, meta in claude_sessions(repo_root(root)).items():
        targets.setdefault(agent, ("claude", meta["socket"]))
    return targets


def try_wake(root: Path, agent: str, direct_only: bool = False) -> str:
    """有未唤醒的新消息且会话空闲 → 注入一轮（不等它跑完，turn 在 daemon 里继续）。
    返回 woken / nothing / busy / unmapped / error:<msg>。"""
    target = wake_targets(root).get(agent)
    if not target:
        return "unmapped"
    kind, address = target
    with state_lock(root):
        state = load_json(bus_dir(root) / STATE_NAME)
        seqs = pending_seqs(root, agent, int(state.get(agent, 0)), direct_only)
        if not seqs:
            return "nothing"
        seq_text = ",".join(str(s) for s in seqs[-8:])
        if kind == "claude":
            try:
                post_claude(address, DEFAULT_WAKE_TEXT.format(seqs=seq_text, agent=agent))
            except OSError as exc:
                return f"error:{exc}"
            state[agent] = seqs[-1]
            save_json(bus_dir(root) / STATE_NAME, state)
            log_wake(root, agent, f"唤醒 #{seq_text}（claude）")
            return "woken"
        thread_id = address
        server = AppServer()
        try:
            if thread_status(server, thread_id) == "active":
                return "busy"
            inject_turn(server, thread_id, DEFAULT_WAKE_TEXT.format(seqs=seq_text, agent=agent))
        except RuntimeError as exc:
            return f"error:{exc}"
        finally:
            server.sock.close()
        state[agent] = seqs[-1]
        save_json(bus_dir(root) / STATE_NAME, state)
        log_wake(root, agent, f"唤醒 #{seq_text}")
        return "woken"


def log_wake(root: Path, agent: str, text: str) -> None:
    log_dir = bus_dir(root) / "wake_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / f"{agent.replace('/', '__')}.log", "a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")


def cmd_map(args) -> int:
    mapping = load_json(bus_dir(args.root) / MAP_NAME)
    mapping[args.agent] = resolve_thread(args.thread)
    save_json(bus_dir(args.root) / MAP_NAME, mapping)
    print(f"✓ {args.agent} → {mapping[args.agent]}")
    return 0


def cmd_unmap(args) -> int:
    mapping = load_json(bus_dir(args.root) / MAP_NAME)
    mapping.pop(args.agent, None)
    save_json(bus_dir(args.root) / MAP_NAME, mapping)
    print(f"✓ 已取消 {args.agent}")
    return 0


def cmd_list(args) -> int:
    mapping = load_json(bus_dir(args.root) / MAP_NAME)
    server = AppServer() if mapping else None
    for agent, thread_id in mapping.items():
        try:
            status = thread_status(server, thread_id)
        except RuntimeError as exc:
            status = f"错误：{exc}"
        print(f"codex   {agent:<28} {thread_id}  {status}")
    for agent, meta in sorted(claude_sessions(repo_root(args.root)).items()):
        if agent not in mapping:
            print(f"claude  {agent:<28} pid {meta['pid']:<7} {meta.get('name', '')}  {meta.get('status', '?')}")
    return 0


def cmd_spawn(args) -> int:
    server = AppServer()
    thread = server.call("thread/start", {"cwd": str(Path(args.cwd).resolve())})["thread"]
    thread_id = thread["id"]
    server.call("thread/name/set", {"threadId": thread_id, "name": args.name})
    server.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": args.text}]})
    mapping = load_json(bus_dir(args.root) / MAP_NAME)
    mapping[args.agent] = thread_id
    save_json(bus_dir(args.root) / MAP_NAME, mapping)
    print(f"✓ 新会话「{args.name}」{thread_id} 已登记为 {args.agent}，首轮已发出", flush=True)
    if args.follow:
        print(f"首轮结束：{server.follow_turn()}")
    return 0


def cmd_wake(args) -> int:
    mapping = load_json(bus_dir(args.root) / MAP_NAME)
    thread_id = mapping.get(args.agent)
    if not thread_id:
        print(f"{args.agent} 未登记映射（先 map 或 spawn）", file=sys.stderr)
        return 2
    server = AppServer()
    status = thread_status(server, thread_id)
    if status == "active" and not args.force:
        print(f"{args.agent} 正忙（active），跳过")
        return 3
    text = args.text or DEFAULT_WAKE_TEXT.format(seqs=args.seqs or "?", agent=args.agent)
    try:
        inject_turn(server, thread_id, text)
    except RuntimeError as exc:
        print(f"唤醒失败：{exc}（若是 active writer，说明会话不在共享 daemon，见 setup_codex.html）", file=sys.stderr)
        return 2
    print(f"✓ 已唤醒 {args.agent}（{thread_id}）", flush=True)
    print(f"本轮结束：{server.follow_turn(quiet=args.quiet)}")
    return 0


def cmd_watch(args) -> int:
    print(f"bus_wake watch：每 {args.interval}s 检查一次（Ctrl-C 退出）", flush=True)
    while True:
        for agent in wake_targets(args.root):
            try:
                result = try_wake(args.root, agent, args.direct_only)
            except SystemExit as exc:  # daemon 暂时连不上：本轮跳过，不退出
                result = f"error:{exc}"
            if result not in ("nothing", "busy"):
                print(f"{time.strftime('%H:%M:%S')} {agent} {result}", flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


def cmd_await(args) -> int:
    """bus.py send 钩子起的后台进程：等对方空闲后叫醒，最多等 --max-wait 秒。"""
    deadline = time.time() + args.max_wait
    while time.time() < deadline:
        try:
            result = try_wake(args.root, args.agent)
        except SystemExit:  # daemon 暂时连不上，当作忙，稍后重试
            result = "busy"
        if result != "busy":
            return 0 if result in ("woken", "nothing", "unmapped") else 2
        time.sleep(args.poll)
    log_wake(args.root, args.agent, f"等待空闲超时（{args.max_wait}s），交给 watch 兜底")
    return 3


def spawn_await(root: Path, recipient: str, sender: str) -> None:
    """供 bus.py send 调用：给已登记的收件人（广播=除发送者外全部）起脱离的后台等待进程。"""
    known = wake_targets(root)
    targets = [a for a in known if a != sender] if recipient == BROADCAST else [recipient]
    for agent in targets:
        if agent not in known:
            continue
        subprocess.Popen(
            [sys.executable, __file__, "--root", str(root), "await", "--agent", agent],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="总线唤醒器：收件箱有新消息时叫醒空闲的 Codex 会话。")
    parser.add_argument("--root", type=Path, default=None, help="协调目录（默认 <git 根>/.agentic）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("map", help="登记 agent ↔ 会话")
    p.add_argument("--agent", required=True)
    p.add_argument("--thread", required=True, help="thread UUID 或会话名")
    p.set_defaults(func=cmd_map)

    p = sub.add_parser("unmap", help="取消登记")
    p.add_argument("--agent", required=True)
    p.set_defaults(func=cmd_unmap)

    p = sub.add_parser("list", help="列出映射与会话状态")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("spawn", help="在 daemon 新开会话并登记")
    p.add_argument("--agent", required=True)
    p.add_argument("--name", required=True, help="会话名（App 侧栏显示）")
    p.add_argument("--text", required=True, help="首轮提示")
    p.add_argument("--cwd", default=".", help="会话工作目录（默认当前目录）")
    p.add_argument("--follow", action="store_true", help="等待首轮结束并打印输出")
    p.set_defaults(func=cmd_spawn)

    p = sub.add_parser("wake", help="叫醒一次")
    p.add_argument("--agent", required=True)
    p.add_argument("--text", default=None, help="自定义提示（默认：去读收件箱）")
    p.add_argument("--seqs", default=None, help=argparse.SUPPRESS)
    p.add_argument("--force", action="store_true", help="会话忙也照发（会被排队或报错）")
    p.add_argument("--quiet", action="store_true", help="不打印 agent 输出")
    p.set_defaults(func=cmd_wake)

    p = sub.add_parser("await", help="（send 钩子内部用）等会话空闲后叫醒")
    p.add_argument("--agent", required=True)
    p.add_argument("--poll", type=int, default=30, help="忙时重查间隔秒（默认 30）")
    p.add_argument("--max-wait", type=int, default=7200, help="最长等待秒（默认 7200）")
    p.set_defaults(func=cmd_await)

    p = sub.add_parser("watch", help="轮询收件箱并自动叫醒（兜底）")
    p.add_argument("--interval", type=int, default=20, help="轮询间隔秒（默认 20）")
    p.add_argument("--direct-only", action="store_true", help="只对点对点消息唤醒，忽略广播")
    p.add_argument("--once", action="store_true", help="只检查一轮（调试用）")
    p.set_defaults(func=cmd_watch)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.root = Path(args.root) if args.root else default_root()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
