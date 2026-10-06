#!/usr/bin/env python3
"""总线唤醒器·收件箱有新消息时叫醒 Codex / Claude Code 会话（通用 Agentic Workflow 版）。

Claude Code 会话：自动发现（~/.claude/sessions/*.json，agent ID 由对话记录推断），往其收件箱
socket 投递消息；需项目 .claude/settings.local.json 设 crossSessionInbound=accept。

Trae 会话：Trae 内装 agentic_workflow/trae_bridge 扩展后，它在 ~/.trae-cn/agentic-bridge.sock
收消息并投进 Trae 当前 agent 会话（内部 IM 桥命令）。agent ID 默认 trae-1，可在
<root>/bus/trae_agent.txt 改。Trae 无空闲查询，忙时消息由 Trae 自己排队，每批新消息只投一次。

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
  until  --agent <id> --task <X[,Y]> [--state done] | --clear
         条件唤醒：等待期间只收点对点/点名/INTERRUPT，条件满足时立即叫醒（task_state 写事件触发 check）
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
import wake_routing  # noqa: E402
from bus import BROADCAST, bus_dir, cursor_path, default_root, inbox_path, iter_jsonl, log_path  # noqa: E402

SOCK = Path.home() / ".codex/app-server-control/app-server-control.sock"
MAP_NAME = "wake_map.json"
RETIRED_NAME = "retired.json"
STATE_NAME = "wake_state.json"
CLAUDE_PAUSE_NAME = "claude_wake.paused"
# 防抖窗口：send 后先等这么久，把同一批广播合并成一次唤醒（AGENTIC_WAKE_DEBOUNCE 可覆盖）
WAKE_DEBOUNCE = int(os.environ.get("AGENTIC_WAKE_DEBOUNCE", "90"))
# 点对点消息（HANDOFF / REQUEST_HELP 等）多半有人在等回复：短防抖，且不被广播的长窗口挡住
DIRECT_DEBOUNCE = int(os.environ.get("AGENTIC_WAKE_DEBOUNCE_DIRECT", "20"))
AWAIT_MAX_WAIT = 7200
TRAE_SOCK = Path.home() / ".trae-cn/agentic-bridge.sock"
TRAE_AGENT_NAME = "trae_agent.txt"
# 与 App「Full access」一致：不审批、无沙箱。spawn 时不传会落回 config.toml 默认（on-request + workspaceWrite，.git 只读）
FULL_ACCESS = {"approvalPolicy": "never", "sandbox": "danger-full-access"}
# turn/start 用的形式（对本轮及之后各轮生效；已加载会话改权限只能走这里）
TURN_FULL_ACCESS = {"approvalPolicy": "never", "sandboxPolicy": {"type": "dangerFullAccess"}}  # 存在即暂停唤醒所有 Claude 会话
DEFAULT_WAKE_TEXT = (
    "【bus_wake 自动唤醒】你的总线收件箱有新消息（{seqs}）。请先跑 "
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


def pending_messages(root: Path, agent: str, after: int, direct_only: bool) -> list[dict]:
    """agent 游标之后、且大于 after 的消息（只读，不推进 bus.py 的游标）。"""
    cursor = cursor_path(root, agent)
    since = int(cursor.read_text(encoding="utf-8").strip() or 0) if cursor.is_file() else 0
    since = max(since, after)
    found = {e.get("seq", 0): e for e in iter_jsonl(inbox_path(root, agent)) if e.get("seq", 0) > since}
    if not direct_only:
        found.update({e.get("seq", 0): e for e in iter_jsonl(log_path(root))
                      if e.get("to") == BROADCAST and e.get("from") != agent and e.get("seq", 0) > since})
    return [found[s] for s in sorted(found)]


def seen_conflicts(root: Path, agent: str, before: int) -> set:
    """游标之前已收到过的 CONFLICT（发送方, 任务）——重复的自动 CONFLICT 不再叫醒。"""
    return {(e.get("from"), e.get("task_id")) for e in iter_jsonl(inbox_path(root, agent))
            if e.get("type") == "CONFLICT" and e.get("seq", 0) <= before}


def plan_wake(root: Path, agent: str, after: int, direct_only: bool) -> tuple[list[dict], str | None]:
    """返回（本次覆盖的全部新消息, 唤醒文案）；文案为 None 表示不值得叫醒。"""
    messages = pending_messages(root, agent, after, direct_only)
    view = wake_routing.load_view(root)
    conditions = wake_routing.load_conditions(root)
    cond = conditions.get(agent)
    if cond and wake_routing.condition_met(view, cond):
        conditions.pop(agent, None)
        wake_routing.save_conditions(root, conditions)
        reason = f"你登记等待的 {'、'.join(cond.get('tasks', []))} 已 {cond.get('state', 'done')}，前置满足"
        return messages, wake_text(root, agent, messages, view, gate=None, reason=reason)
    if not messages:
        return [], None
    text = wake_text(root, agent, messages, view, gate=cond)
    return messages, text


def inject_turn(server: AppServer, thread_id: str, text: str) -> None:
    server.call("thread/resume", {"threadId": thread_id, "excludeTurns": True})
    server.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": text}], **TURN_FULL_ACCESS})


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


def trae_call(req: dict, timeout: float = 15) -> dict:
    """调 Trae 桥扩展（一行 JSON 请求 → 一行 JSON 回复）。"""
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(timeout)
        conn.connect(str(TRAE_SOCK))
        conn.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode())
        return json.loads(conn.makefile(encoding="utf-8").readline() or "{}")


def trae_agent(root: Path) -> str | None:
    """桥在线且 Trae 窗口打开的是本仓库 → 返回登记的 Trae agent ID。"""
    if not TRAE_SOCK.exists():
        return None
    try:
        info = trae_call({"op": "ping"}, timeout=3)
    except (OSError, ValueError):
        return None
    if str(repo_root(root)) not in info.get("workspace", []):
        return None
    name_file = bus_dir(root) / TRAE_AGENT_NAME
    return (name_file.read_text().strip() if name_file.exists() else "") or "trae-1"


def repo_root(root: Path) -> Path:
    return root.resolve().parent


def broadcast_audience_of(root: Path) -> set[str]:
    """广播受众 = 车道 agent + 协调人 + wake_map 显式登记的会话；解析失败时退回只叫显式登记的。"""
    explicit = set(load_json(bus_dir(root) / MAP_NAME))
    try:
        import plans as plans_mod
        return explicit | plans_mod.broadcast_audience(root)
    except Exception:  # noqa: BLE001
        return explicit


def wake_targets(root: Path) -> dict[str, tuple[str, str]]:
    """{agent_id: ("codex", thread_id) | ("claude", socket) | ("trae", "")}；Codex 来自映射，Claude/Trae 自动发现。"""
    targets = {agent: ("codex", thread) for agent, thread in load_json(bus_dir(root) / MAP_NAME).items()}
    trae = trae_agent(root)
    if trae:
        targets.setdefault(trae, ("trae", ""))
    if (bus_dir(root) / CLAUDE_PAUSE_NAME).exists():
        return targets  # 用户暂停了 Claude 唤醒（如快到会话额度）：只叫 Codex
    for agent, meta in claude_sessions(repo_root(root)).items():
        targets.setdefault(agent, ("claude", meta["socket"]))
    return targets


def wake_text(root: Path, agent: str, messages: list[dict], view, gate: dict | None,
              reason: str | None = None) -> str | None:
    """唤醒文案：消息摘要直接内联（已代读，免去 bus.py read 一轮）；无相关消息返回 None。
    agent 在多个 active 计划有车道时提醒带 --plan。"""
    first = messages[0].get("seq", 0) if messages else 0
    conflicts = seen_conflicts(root, agent, first - 1) if messages else set()
    relevant, skipped = [], []
    for msg in messages:
        why = None if wake_routing.gated(gate, msg, agent) else wake_routing.relevance(view, agent, msg, conflicts)
        (relevant.append((msg, why)) if why else skipped.append(msg))
    if not relevant and not reason:
        return None
    head = f"【bus_wake 自动唤醒】{reason}。" if reason else "【bus_wake 自动唤醒】你有与你相关的新总线消息。"
    text = (
        f"{head}以下已代读（收件箱游标已推进，无需再跑 bus.py read）：\n"
        f"{wake_routing.digest(relevant, skipped)}\n"
        "按 AGENTS.md 车道规矩处理：前置满足就继续车道；仍需等待就跑 "
        f"`python3 agentic_workflow/tools/bus_wake.py until --agent {agent} --task <在等的任务>` 登记条件后，"
        "一句话说明在等什么并结束本轮（条件满足前不会再因无关广播被叫醒）。"
    )
    if view is not None and agent in view.coords:
        try:
            import token_usage
            ctx = token_usage.current_context(root, agent)
        except Exception:  # noqa: BLE001  账本缺失不影响唤醒
            ctx = 0
        if ctx > token_usage.ROTATE_CTX:
            text += (f"\n上下文（1d）：你的会话上下文已约 {ctx // 1000}k token，每次调用都在重放它。处理完本轮后把"
                     "未结事项写进 Doc/godot/handoffs/<日期>-coord-handoff.md，然后请用户开新会话接任"
                     "（沿用同一协调人 ID，新会话先读交接文件）。")
    try:
        import plans as plans_mod
        lanes_in = plans_mod.agent_plans(root).get(agent, [])
    except Exception:  # noqa: BLE001  计划解析失败不影响唤醒
        lanes_in = []
    if len(lanes_in) > 1:
        text += f" 注意：你在 {'、'.join(lanes_in)} 都有车道，task_state.py 命令请带 --plan <计划名>，按消息标注的计划处理。"
    return text


def mark_delivered(root: Path, agent: str, state: dict, messages: list[dict]) -> None:
    if messages:
        last = max(int(m.get("seq", 0)) for m in messages)
        state[agent] = max(int(state.get(agent, 0)), last)
        cursor = cursor_path(root, agent)
        cursor.parent.mkdir(parents=True, exist_ok=True)
        cursor.write_text(str(last), encoding="utf-8")
    save_json(bus_dir(root) / STATE_NAME, state)


def deliver(agent: str, target: tuple[str, str], text: str) -> str:
    """把一段文字作为新一轮投给会话：woken / busy / error:<msg>。"""
    kind, address = target
    if kind == "trae":
        try:
            reply = trae_call({"wait": False, "text": text})
        except (OSError, ValueError) as exc:
            return f"error:{exc}"
        if not reply.get("ok"):
            return "busy" if reply.get("busy") else f"error:{reply.get('error')}"
        return "woken"
    if kind == "claude":
        try:
            post_claude(address, text)
        except OSError as exc:
            return f"error:{exc}"
        return "woken"
    server = AppServer()
    try:
        if thread_status(server, address) == "active":
            return "busy"
        inject_turn(server, address, text)
    except RuntimeError as exc:
        return f"error:{exc}"
    finally:
        server.sock.close()
    return "woken"


def try_wake(root: Path, agent: str, direct_only: bool = False) -> str:
    """有值得叫醒的新消息（或条件满足）且会话空闲 → 注入一轮（不等它跑完）。
    返回 woken / nothing / busy / unmapped / error:<msg>。"""
    target = wake_targets(root).get(agent)
    if not target:
        return "unmapped"
    with state_lock(root):
        state = load_json(bus_dir(root) / STATE_NAME)
        messages, text = plan_wake(root, agent, int(state.get(agent, 0)), direct_only)
        if text is None:
            return "nothing"
        result = deliver(agent, target, text)
        if result == "woken":
            mark_delivered(root, agent, state, messages)
            seq_text = ",".join(str(m.get("seq")) for m in messages[-8:]) or "条件"
            log_wake(root, agent, f"唤醒 #{seq_text}（{target[0]}）")
        return result


def retire_finished(root: Path) -> None:
    """2b：车道已全部 done 的 agent 通知一次「可以结束会话」，之后只会因点对点消息被叫醒。
    车道又加了新任务（不再 finished）时自动取消退场记录。"""
    view = wake_routing.load_view(root)
    if view is None:
        return
    path = bus_dir(root) / RETIRED_NAME
    retired = load_json(path)
    changed = False
    for agent, target in wake_targets(root).items():
        finished = view.lane_finished(agent) and agent not in view.coords
        if not finished:
            if retired.pop(agent, None) is not None:
                changed = True
            continue
        if agent in retired:
            continue
        text = ("【bus_wake 车道收工】你在 active 计划里的车道任务已全部 done。按规矩收尾：确认改动都已提交、"
                "task_state 备注写清验收等级后结束本会话即可。之后与你无关的广播不会再叫醒你；"
                "有人点对点找你时仍会叫醒。")
        if deliver(agent, target, text) == "woken":
            retired[agent] = time.strftime("%Y-%m-%d %H:%M:%S")
            log_wake(root, agent, "车道收工通知（退场）")
            changed = True
    if changed:
        save_json(path, retired)


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
    trae = trae_agent(args.root)
    if trae and trae not in mapping:
        print(f"trae    {trae:<28} {TRAE_SOCK}")
    return 0


def cmd_spawn(args) -> int:
    server = AppServer()
    thread = server.call("thread/start", {"cwd": str(Path(args.cwd).resolve()), **FULL_ACCESS})["thread"]
    thread_id = thread["id"]
    server.call("thread/name/set", {"threadId": thread_id, "name": args.name})
    server.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": args.text}], **TURN_FULL_ACCESS})
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
    if not thread_id and args.agent == trae_agent(args.root):
        if not args.text:  # 不带 --text：按收件箱新消息叫醒（推进游标）
            result = try_wake(args.root, args.agent)
            print(f"{args.agent}（trae）：{result}")
            return 0 if result in ("woken", "nothing") else 2
        reply = trae_call({"wait": False, "text": args.text})
        print(f"{args.agent}（trae）：{reply}")
        return 0 if reply.get("ok") else 2
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
        try:
            retire_finished(args.root)
        except Exception as exc:  # noqa: BLE001  退场通知是增强，不能拖垮 watch
            print(f"{time.strftime('%H:%M:%S')} 退场检查出错：{exc}", flush=True)
        try:
            import review_runner
            if review_runner.due(args.root):
                review_runner.spawn(args.root)
                print(f"{time.strftime('%H:%M:%S')} 启动验收 agent", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"{time.strftime('%H:%M:%S')} 验收检查出错：{exc}", flush=True)
        audience = broadcast_audience_of(args.root)
        for agent in wake_targets(args.root):
            try:
                result = try_wake(args.root, agent, args.direct_only or agent not in audience)
            except SystemExit as exc:  # daemon 暂时连不上：本轮跳过，不退出
                result = f"error:{exc}"
            if result not in ("nothing", "busy"):
                print(f"{time.strftime('%H:%M:%S')} {agent} {result}", flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


def pending_marker(root: Path, agent: str) -> Path:
    return bus_dir(root) / "wake_pending" / agent.replace("/", "__")


def cmd_await(args) -> int:
    """bus.py send 钩子起的后台进程：先防抖合并一批消息，再等对方空闲后叫醒，最多等 --max-wait 秒。"""
    marker = pending_marker(args.root, args.agent)
    try:
        time.sleep(max(0, args.debounce))  # 窗口内后续 send 不再起新进程，消息并入这一次唤醒
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
    finally:
        if args.debounce > 0:
            with contextlib.suppress(OSError):
                marker.unlink()


def spawn_await(root: Path, recipient: str, sender: str, message_type: str = "") -> None:
    """供 bus.py send 调用：给已登记的收件人（广播=受众里除发送者外）起脱离的后台等待进程。
    防抖：广播 90s，窗口内该收件人已有等待进程就不再起；点对点 20s 且单独起；INTERRUPT 立即叫。
    是否真叫醒由 try_wake 按路由规则决定（与其无关的广播不会叫醒）。"""
    known = wake_targets(root)
    if recipient == BROADCAST:
        audience = broadcast_audience_of(root)
        targets = [a for a in known if a != sender and a in audience]
    else:
        targets = [recipient]
    urgent = message_type == "INTERRUPT"
    direct = recipient != BROADCAST
    debounce = 0 if urgent else DIRECT_DEBOUNCE if direct else WAKE_DEBOUNCE
    for agent in targets:
        if agent not in known:
            continue
        if not urgent and not direct:
            marker = pending_marker(root, agent)
            with contextlib.suppress(OSError):
                if marker.is_file() and time.time() - marker.stat().st_mtime < debounce + AWAIT_MAX_WAIT:
                    continue
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(str(os.getpid()), encoding="utf-8")
        subprocess.Popen(
            [sys.executable, __file__, "--root", str(root), "await", "--agent", agent, "--debounce", str(debounce)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def cmd_until(args) -> int:
    """登记 / 清除条件唤醒：在等的任务全部到达目标状态前，只收点对点、点名与 INTERRUPT。"""
    conditions = wake_routing.load_conditions(args.root)
    if args.clear:
        conditions.pop(args.agent, None)
        wake_routing.save_conditions(args.root, conditions)
        print(f"✓ 已清除 {args.agent} 的条件唤醒")
        return 0
    tasks = [t.strip() for t in (args.task or "").split(",") if t.strip()]
    if not tasks:
        print("需要 --task <任务[,任务…]> 或 --clear", file=sys.stderr)
        return 2
    conditions[args.agent] = {"tasks": tasks, "state": args.state, "since": time.strftime("%Y-%m-%d %H:%M:%S")}
    wake_routing.save_conditions(args.root, conditions)
    print(f"✓ {args.agent} 等待 {'、'.join(tasks)} → {args.state}；满足前无关广播不叫醒你")
    return 0


def cmd_check(args) -> int:
    """task_state 写事件后的钩子：给条件已满足的 agent 立即叫醒（会话忙则起后台等待）。"""
    view = wake_routing.load_view(args.root)
    conditions = wake_routing.load_conditions(args.root)
    if args.done and view is not None:  # 刚 done 的任务真正解锁了谁：给其车道 agent 补一条已满足的条件
        known = wake_targets(args.root)
        for task in sorted(view.succs.get(args.done, ())):
            owner = view.owner.get(task)
            if owner in known and view.state(task) in ("pending", "claimed", "blocked") and view.unlocked(task):
                conditions.setdefault(owner, {"tasks": [args.done], "state": "done", "since": "auto"})
        wake_routing.save_conditions(args.root, conditions)
    for agent, cond in conditions.items():
        if not wake_routing.condition_met(view, cond):
            continue
        result = try_wake(args.root, agent)
        if result == "busy":
            subprocess.Popen(
                [sys.executable, __file__, "--root", str(args.root), "await", "--agent", agent, "--debounce", "0"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        print(f"{agent}：{result}")
    return 0


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
    p.add_argument("--max-wait", type=int, default=AWAIT_MAX_WAIT, help=f"最长等待秒（默认 {AWAIT_MAX_WAIT}）")
    p.add_argument("--debounce", type=int, default=0, help="先等这么多秒合并后续消息（默认 0）")
    p.set_defaults(func=cmd_await)

    p = sub.add_parser("until", help="条件唤醒：等某些任务到达状态前，不因无关广播被叫醒")
    p.add_argument("--agent", required=True)
    p.add_argument("--task", default=None, help="在等的任务，逗号分隔")
    p.add_argument("--state", default="done", help="目标状态（默认 done）")
    p.add_argument("--clear", action="store_true", help="清除条件")
    p.set_defaults(func=cmd_until)

    p = sub.add_parser("check", help="（task_state 钩子内部用）叫醒条件已满足的 agent")
    p.add_argument("--done", default=None, help="刚 done 的任务：顺带叫醒因它解锁的后继车道 agent")
    p.set_defaults(func=cmd_check)

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
