#!/usr/bin/env python3
"""done 当场的机械检查：task_state done 时用确定性规则核对备注，有问题立刻提示车道当场补。

只做提示，不判验收（验收由 review_runner 的验收 agent 做）。检查项：
1. 备注写了验收等级（L0 / L1 / L2）。
2. 备注里至少有一个本任务的提交：能解析成 commit，且至少改了一个计划登记的文件
   （备注里顺带提到的基线 SHA——一个登记文件都没碰——不算本任务提交，忽略）。
3. 本任务提交改动的文件都在计划登记的「改动文件」里；测试（tests/、test_*）、flow 脚本（flows/）、
   .uid 伴生文件、.agentic/ 视图豁免。热点文件（main.gd、main.tscn、test_runner.gd、verify.sh、
   parity-matrix.md）不豁免——按规矩只在收尾集成改，碰了就该人看。
证据路径（out/...）常在已删除的临时 worktree 里，只作提示（notes），不判不过。

done 仍是「一盏灯」：检查结果只提示、记进事件的 lint 字段，不拦截状态写入。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

LEVEL_RE = re.compile(r"\bL[0-2]\b")
SHA_RE = re.compile(r"(?<![0-9a-zA-Z/._-])([0-9a-f]{7,40})(?![0-9a-zA-Z_-])")
EVIDENCE_RE = re.compile(r"(?<![\w/])(out/[^\s，。；;,、）)」（({]+)")
EXEMPT_RE = re.compile(r"(^|/)(tests?|flows)/|(^|/)test_[^/]*$|^\.agentic/")
HOT_RE = re.compile(r"(^|/)(main\.gd|main\.tscn|test_runner\.gd|verify\.sh|parity-matrix\.md)$")


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def commit_files(repo: Path, sha: str) -> list[str] | None:
    if git(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
        return None
    out = git(repo, "show", "--name-only", "--pretty=format:", sha).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def clean_spec(spec: str) -> str:
    """计划单元格里常带注释（`a.gd`（HOT 接线 …））：只取路径部分。"""
    return re.split(r"[`（(\s]", spec.strip().strip("`"), maxsplit=1)[0]


def planned_hit(path: str, planned: list[str], spec_hits_path) -> bool:
    base = path[:-4] if path.endswith(".uid") else path
    return any(spec_hits_path(clean_spec(spec), base) for spec in planned if clean_spec(spec))


def check(repo: Path, note: str, planned: list[str], spec_hits_path) -> tuple[list[str], list[str]]:
    """返回（不通过原因, 提示）；原因为空 = 通过。spec_hits_path(spec, path) 来自 task_state（支持 glob / 目录）。"""
    reasons: list[str] = []
    notes: list[str] = []
    note = note or ""
    if not LEVEL_RE.search(note):
        reasons.append("备注没写验收等级（L0/L1/L2）")
    changed: list[str] = []
    own = 0
    for sha in dict.fromkeys(SHA_RE.findall(note)):
        files = commit_files(repo, sha)
        if files is None or not any(planned_hit(f, planned, spec_hits_path) for f in files):
            continue  # 不是提交，或是顺带提到的基线提交
        own += 1
        changed += files
    if not own:
        reasons.append("备注里没有本任务的提交 SHA（能解析且改了计划登记文件的）")
    stray, hot = [], []
    for path in dict.fromkeys(changed):
        if planned_hit(path, planned, spec_hits_path):
            continue
        if HOT_RE.search(path):
            hot.append(path)
        elif not EXEMPT_RE.search(path):
            stray.append(path)
    if hot:
        reasons.append(f"改了未登记的热点文件：{'、'.join(hot)}")
    if stray:
        reasons.append(f"提交改了计划外的文件：{'、'.join(stray[:5])}" + (f" 等 {len(stray)} 个" if len(stray) > 5 else ""))
    evidence = [p.rstrip(".") for p in EVIDENCE_RE.findall(note)]
    missing = [p for p in evidence if not (repo / p).exists()]
    if missing:
        notes.append(f"证据路径不在当前工作树（可能在临时 worktree）：{'、'.join(missing[:3])}")
    return reasons, notes
