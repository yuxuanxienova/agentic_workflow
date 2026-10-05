"""多计划目录（task_state / bus 共用）。

每个计划一个独立文件夹，计划之间互不混写：

  <root>/plans/<name>/
    plan.json      {"name", "source", "status": "active"|"closed", "opened", "closed"}
    plan.md        计划源文件（source）的副本——工具只解析这份；active 计划每次写事件时自动与 source 同步，
                   closed 计划冻结为归档快照
    events.jsonl   本计划的状态事件（append-only，唯一状态真源）
    board.md / gantt.md / reconcile.md   生成物，勿手改
  <root>/index.md  所有计划一行汇总（生成物）
  <root>/bus/      消息总线（全局一份，见 bus.py）

计划选择：--plan <name> > 环境变量 AGENTIC_PLAN > 按任务 ID 在 active 计划里唯一匹配 > 唯一的 active 计划。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

PLANS_DIR = "plans"
META_NAME = "plan.json"
DOC_NAME = "plan.md"
ENV_PLAN = "AGENTIC_PLAN"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def plans_root(root: Path) -> Path:
    return root / PLANS_DIR


def plan_dir(root: Path, name: str) -> Path:
    return plans_root(root) / name


def read_meta(pdir: Path) -> dict:
    try:
        return json.loads((pdir / META_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_meta(pdir: Path, meta: dict) -> None:
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / META_NAME).write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def all_plans(root: Path) -> list[tuple[str, Path, dict]]:
    base = plans_root(root)
    if not base.is_dir():
        return []
    out = []
    for pdir in sorted(base.iterdir()):
        if pdir.is_dir() and (pdir / META_NAME).is_file():
            out.append((pdir.name, pdir, read_meta(pdir)))
    return out


def active_plans(root: Path) -> list[tuple[str, Path, dict]]:
    return [p for p in all_plans(root) if p[2].get("status") == "active"]


def sync_copy(repo: Path, pdir: Path) -> str | None:
    """active 计划：source 与副本不同则覆盖副本；返回提示文本（无变化返回 None）。"""
    meta = read_meta(pdir)
    if meta.get("status") != "active" or not meta.get("source"):
        return None
    source = repo / meta["source"]
    copy = pdir / DOC_NAME
    if not source.is_file():
        return f"计划源文件不存在：{meta['source']}（沿用副本 {copy}）"
    if copy.is_file() and copy.read_bytes() == source.read_bytes():
        return None
    shutil.copyfile(source, copy)
    return f"已同步计划副本 {meta['source']} → {copy}"


def resolve(root: Path, name: str | None, task: str | None, task_ids_of) -> tuple[str | None, str]:
    """返回 (计划名, 错误信息)。task_ids_of(pdir) → 该计划文档里的任务 ID 集合。"""
    name = name or os.environ.get(ENV_PLAN) or None
    if name:
        if not (plan_dir(root, name) / META_NAME).is_file():
            known = ", ".join(n for n, _, _ in all_plans(root)) or "（无）"
            return None, f"未知计划「{name}」；已有：{known}"
        return name, ""
    active = active_plans(root)
    if task:
        hits = [n for n, pdir, _ in active if task in task_ids_of(pdir)]
        if len(hits) == 1:
            return hits[0], ""
        if len(hits) > 1:
            return None, f"{task} 同时出现在 active 计划 {', '.join(hits)} 里，请用 --plan <name> 指定"
    if len(active) == 1:
        return active[0][0], ""
    if not active:
        return None, f"没有 active 计划：先 `task_state.py plan open <name> --source <计划文档>`"
    names = ", ".join(n for n, _, _ in active)
    where = f"{task} 不在任何 active 计划里；" if task else ""
    return None, f"{where}有多个 active 计划（{names}），请用 --plan <name> 或 export {ENV_PLAN}=<name>"
