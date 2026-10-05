#!/usr/bin/env python3
"""车道（派工）与同步点（跨车道依赖）的解析、校验与排期（通用 Agentic Workflow 版）。

三个概念各管一件事（混用即乱）：
  链   约束：改同一文件的任务 = 一条串行链。工具从「改动文件」列自动算，不手写。
  车道 分配：一个 agent 一条车道，车道内按「任务顺序」从头做到尾。唯一派工单位。
  波次 时间：由「车道顺序 + 同步点」推导出的时间槽（W1 = 无前置可立即开工）。不再手写。

计划文档里需要两张表（列标题逐字匹配）：

  | 车道 | agent | 任务顺序 | 说明 |
  |---|---|---|---|
  | A1 | claude-x-1 | `P6-2` → `P7-4` → `P7-5` | 战斗·开火 |

  | 后（等待方） | 先（被等任务） | 原因 |
  |---|---|---|
  | `P7-10` | `P6-2` | combat_status_effects.gd 由 P6-2 新建 |

校验规则（validate）：
  1. 每个计划任务恰好出现在一条车道里；车道/同步表不得引用未知任务。
  2. 依赖图（车道相邻 + 同步点）无环。
  3. **跨车道共享文件必须被全序化**：分属不同车道且改动文件重叠的两个任务，
     依赖图里必须有一条路径把它们排成先后，否则可能被两个 agent 同时改 → 报错。
  同车道内天然串行，无需同步点。

退出码（CLI）：0 通过；1 校验失败；2 用法或数据错误。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

LANE_HEADER = ("车道", "agent", "任务顺序")
SYNC_AFTER = "后（等待方）"
SYNC_BEFORE = "先（被等任务）"
TASK_TOKEN = re.compile(r"[A-Z]+\d+-\d+")


def _split(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_sep(cells: list[str]) -> bool:
    return bool(cells) and all(c and set(c) <= set("-: ") for c in cells)


def _tables(text: str):
    """逐张表产出 (表头, [数据行])。"""
    header, rows, seen_sep = None, [], False
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            if header is not None and seen_sep:
                yield header, rows
            header, rows, seen_sep = None, [], False
            continue
        cells = _split(line)
        if header is None:
            header = cells
        elif not seen_sep and _is_sep(cells):
            seen_sep = True
        elif seen_sep:
            rows.append(cells)
    if header is not None and seen_sep:
        yield header, rows


def parse_lanes(plan_path: Path) -> dict[str, dict]:
    """{车道: {agent, tasks:[...], note}}，保持文档顺序。无车道表返回 {}。"""
    lanes: dict[str, dict] = {}
    text = plan_path.read_text(encoding="utf-8").lstrip("﻿")
    for header, rows in _tables(text):
        if not all(h in header for h in LANE_HEADER):
            continue
        i_lane, i_agent, i_tasks = (header.index(h) for h in LANE_HEADER)
        i_note = header.index("说明") if "说明" in header else None
        for cells in rows:
            if len(cells) <= max(i_lane, i_agent, i_tasks):
                continue
            lane = cells[i_lane].strip("`* ")
            if not lane:
                continue
            agent = cells[i_agent].strip("` ")
            lanes[lane] = {
                "agent": "" if agent in ("", "—", "-", "待定") else agent,
                "tasks": TASK_TOKEN.findall(cells[i_tasks]),
                "note": cells[i_note] if i_note is not None and i_note < len(cells) else "",
            }
    return lanes


def parse_sync(plan_path: Path) -> list[dict]:
    """[{after, before:[...], reason}]。"""
    out: list[dict] = []
    text = plan_path.read_text(encoding="utf-8").lstrip("﻿")
    for header, rows in _tables(text):
        if SYNC_AFTER not in header or SYNC_BEFORE not in header:
            continue
        i_after, i_before = header.index(SYNC_AFTER), header.index(SYNC_BEFORE)
        i_reason = header.index("原因") if "原因" in header else None
        for cells in rows:
            if len(cells) <= max(i_after, i_before):
                continue
            afters = TASK_TOKEN.findall(cells[i_after])
            befores = TASK_TOKEN.findall(cells[i_before])
            reason = cells[i_reason] if i_reason is not None and i_reason < len(cells) else ""
            for after in afters:
                out.append({"after": after, "before": befores, "reason": reason})
    return out


def lane_of(lanes: dict[str, dict]) -> dict[str, str]:
    return {t: lane for lane, info in lanes.items() for t in info["tasks"]}


def predecessors(lanes: dict[str, dict], sync: list[dict]) -> dict[str, set[str]]:
    """{任务: 直接前置集合}（车道内前一个 + 同步点）。"""
    preds: dict[str, set[str]] = {}
    for info in lanes.values():
        prev = None
        for t in info["tasks"]:
            preds.setdefault(t, set())
            if prev:
                preds[t].add(prev)
            prev = t
    for edge in sync:
        preds.setdefault(edge["after"], set()).update(edge["before"])
        for b in edge["before"]:
            preds.setdefault(b, set())
    return preds


def schedule(preds: dict[str, set[str]]) -> tuple[dict[str, int], list[str]]:
    """单位工期最长路径排期 → ({任务: 波次序号(1 起)}, 环上任务)。"""
    slot: dict[str, int] = {}
    state: dict[str, int] = {}
    cyclic: list[str] = []

    def visit(t: str) -> int:
        if state.get(t) == 2:
            return slot[t]
        if state.get(t) == 1:
            cyclic.append(t)
            return 0
        state[t] = 1
        slot[t] = 1 + max((visit(p) for p in preds.get(t, ())), default=0)
        state[t] = 2
        return slot[t]

    for t in sorted(preds):
        visit(t)
    return slot, cyclic


def ancestors(preds: dict[str, set[str]]) -> dict[str, set[str]]:
    memo: dict[str, set[str]] = {}

    def anc(t: str, stack: frozenset = frozenset()) -> set[str]:
        if t in memo:
            return memo[t]
        if t in stack:
            return set()
        acc: set[str] = set()
        for p in preds.get(t, ()):
            acc.add(p)
            acc |= anc(p, stack | {t})
        memo[t] = acc
        return acc

    for t in preds:
        anc(t)
    return memo


def validate(plan_files: dict[str, list[str]], lanes: dict[str, dict], sync: list[dict],
             shared_files) -> list[str]:
    """返回错误列表；空 = 通过。shared_files 由 check_file_ownership 提供。"""
    errors: list[str] = []
    where = lane_of(lanes)
    seen: dict[str, str] = {}
    for lane, info in lanes.items():
        for t in info["tasks"]:
            if t in seen:
                errors.append(f"{t} 同时出现在车道 {seen[t]} 与 {lane}")
            seen[t] = lane
            if t not in plan_files:
                errors.append(f"车道 {lane} 引用了计划里不存在的任务 {t}")
    for t in sorted(plan_files):
        if t not in where:
            errors.append(f"{t} 未分配到任何车道")
    for edge in sync:
        for t in [edge["after"], *edge["before"]]:
            if t not in plan_files:
                errors.append(f"同步点引用了未知任务 {t}")
    preds = predecessors(lanes, sync)
    _, cyclic = schedule(preds)
    if cyclic:
        errors.append(f"依赖图有环（车道顺序与同步点矛盾）：{', '.join(sorted(set(cyclic)))}")
        return errors
    anc = ancestors(preds)
    ids = sorted(t for t in plan_files if t in where)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if where[a] == where[b]:
                continue
            hits = shared_files(plan_files[a], plan_files[b])
            if hits and a not in anc.get(b, set()) and b not in anc.get(a, set()):
                errors.append(
                    f"跨车道共享文件未排序：{a}（{where[a]}）↔ {b}（{where[b]}）：{'；'.join(hits)}"
                    " → 加同步点或归同一车道"
                )
    return errors


def components(plan_files: dict[str, list[str]], shared_files) -> list[list[str]]:
    """按「改动文件重叠」做连通分量（= 合并后的链组），大的在前。"""
    ids = sorted(plan_files)
    parent = {t: t for t in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if shared_files(plan_files[a], plan_files[b]):
                parent[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for t in ids:
        groups.setdefault(find(t), []).append(t)
    return sorted(groups.values(), key=lambda g: (-len(g), g[0]))


def main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import check_file_ownership as cfo

    parser = argparse.ArgumentParser(description="校验车道与同步点，并打印推导出的波次。")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--components", action="store_true", help="打印按文件重叠合并的链组（规划车道用）")
    args = parser.parse_args(argv)
    if not args.plan.is_file():
        print(f"找不到计划文档：{args.plan}", file=sys.stderr)
        return 2
    plan_files = cfo.parse_plan(args.plan, cfo.DEFAULT_HEADER, re.compile(cfo.DEFAULT_ID_PATTERN))
    if args.components:
        for group in components(plan_files, cfo.shared_files):
            print(f"[{len(group)}] {' '.join(group)}")
        return 0
    lanes, sync = parse_lanes(args.plan), parse_sync(args.plan)
    if not lanes:
        print("✗ 计划里没有车道表（表头须含：车道 | agent | 任务顺序）", file=sys.stderr)
        return 2
    errors = validate(plan_files, lanes, sync, cfo.shared_files)
    if errors:
        print(f"✗ 车道校验失败（{len(errors)} 项）")
        for e in errors:
            print(f"  {e}")
        return 1
    slot, _ = schedule(predecessors(lanes, sync))
    print(f"✓ 车道校验通过：{len(lanes)} 条车道、{len(plan_files)} 个任务、{len(sync)} 个同步点、"
          f"推导 {max(slot.values())} 个波次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
