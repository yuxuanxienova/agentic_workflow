#!/usr/bin/env python3
"""分派前的「文件所有权矩阵」重叠校验（通用 Agentic Workflow 版）。

数据源是唯一真源：计划文档里每个子任务行末的「改动文件」列（Markdown 表格）。
本脚本不维护第二份数据。传入若干任务 ID 即两两比较，只要出现同一文件
（含目录前缀与 `*`/`?`/`[` 通配）即报冲突并退出 1；无重叠退出 0。

用法示例：
  # 校验一个准备同波并行的任务集合；有文件重叠即报错并退出 1
  python3 tools/check_file_ownership.py P1-1 P2-1 P3-1 P4-7

  # 打印全量矩阵与所有现存重叠对，供人工复核
  python3 tools/check_file_ownership.py --all

  # 只看某个任务会碰哪些文件
  python3 tools/check_file_ownership.py --explain P4-2

  # 校验车道表：任务全覆盖、无环、跨车道共享文件已被同步点排序（见 lanes.py）
  python3 tools/check_file_ownership.py --plan PLAN.md --lanes

  # 打印按文件重叠合并的链组（规划车道时用来装箱）
  python3 tools/check_file_ownership.py --plan PLAN.md --components

  # 自定义计划文档 / 列标题 / 任务 ID 正则
  python3 tools/check_file_ownership.py --plan ./PLAN.md --header '改动文件' \
      --id-pattern '^T-\\d+$' T-1 T-2

重叠判定同时覆盖精确路径、目录前缀（以 / 结尾）与 `*`/`?`/`[` 通配：
  `Godot/tools/flows/tech_*.json` 与 `Godot/tools/flows/tech_detail_scroll_p1_7.json` 判为重叠；
  `out/godot/parity/shipyard/` 与 `out/godot/parity/shipyard/ratio/` 判为重叠。

退出码：0 无冲突；1 存在文件所有权冲突；2 用法或数据错误。
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_PLAN = Path("./PLAN.md")
DEFAULT_HEADER = "改动文件（仓库相对路径）"
DEFAULT_ID_PATTERN = r"^[A-Z]+\d+-\d+$"
GLOB_CHARS = set("*?[")


def infer_repo_root(start: Path) -> Path:
    """从 start 向上逐级查找包含 .git 的目录作为仓库根；找不到回退 start。"""
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return current


def split_cells(line: str) -> list[str]:
    """把 `| a | b |` 拆成 ['a', 'b']（去掉首尾空单元）。"""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def is_separator_row(cells: list[str]) -> bool:
    """判断 Markdown 表格的分隔行（如 `| --- | :--- |`）。"""
    return bool(cells) and all(cell and set(cell) <= set("-: ") for cell in cells)


def clean_file_token(raw: str) -> str:
    """清理单个文件标记：去空白、去反引号、去 `./` 前缀。"""
    token = raw.strip().strip("`").strip()
    if token.startswith("./"):
        token = token[2:]
    return token


def parse_file_cell(cell: str) -> list[str]:
    """按中文/英文分号拆分单元格，得到文件路径列表。"""
    tokens: list[str] = []
    for part in re.split(r"[；;]", cell):
        token = clean_file_token(part)
        if token:
            tokens.append(token)
    return tokens


def parse_plan(plan_path: Path, header: str, id_re: re.Pattern) -> dict[str, list[str]]:
    """从计划文档解析 {任务 ID: [文件路径, ...]}。

    参数：
      plan_path: 计划文档路径。
      header:    「改动文件」列标题（逐字匹配，避免误绑其它模板表）。
      id_re:     任务 ID 正则，只有首单元格匹配的行才被视为矩阵数据行。
    """
    text = plan_path.read_text(encoding="utf-8").lstrip("\ufeff")
    ownership: dict[str, list[str]] = {}
    col_index: int | None = None

    for line in text.splitlines():
        if not line.strip().startswith("|"):
            col_index = None
            continue
        cells = split_cells(line)
        if header in cells:
            col_index = cells.index(header)
            continue
        if col_index is None or is_separator_row(cells):
            continue
        if len(cells) <= col_index:
            continue
        task_id = cells[0].replace("✅", "").strip()
        if not id_re.match(task_id):
            continue
        files = parse_file_cell(cells[col_index])
        if files:
            ownership[task_id] = files
    return ownership


def is_glob(spec: str) -> bool:
    """判断某个路径规格是否含通配符。"""
    return any(ch in spec for ch in GLOB_CHARS)


def specs_overlap(a: str, b: str) -> bool:
    """判断两个路径规格是否可能指向同一文件/目录。

    覆盖：精确相等、双通配（fnmatch）、目录前缀（以 / 结尾）。
    """
    if a == b:
        return True
    if is_glob(a) and fnmatch.fnmatch(b, a):
        return True
    if is_glob(b) and fnmatch.fnmatch(a, b):
        return True
    if a.endswith("/") and (b == a[:-1] or b.startswith(a)):
        return True
    if b.endswith("/") and (a == b[:-1] or a.startswith(b)):
        return True
    return False


def shared_files(files_a: list[str], files_b: list[str]) -> list[str]:
    """返回两组文件规格之间所有重叠项，形如 `x ≈ y`。"""
    hits: list[str] = []
    for x in files_a:
        for y in files_b:
            if specs_overlap(x, y):
                hits.append(x if x == y else f"{x} ≈ {y}")
    return hits


def find_conflicts(ownership: dict[str, list[str]], task_ids: list[str]) -> list[dict]:
    """两两比较给定任务集合，返回所有重叠对。"""
    conflicts: list[dict] = []
    ids = sorted(task_ids)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            hits = shared_files(ownership[a], ownership[b])
            if hits:
                conflicts.append({"a": a, "b": b, "files": hits})
    return conflicts


def print_all(ownership: dict[str, list[str]], repo: Path) -> None:
    """打印全量所有权矩阵与所有现存重叠对。"""
    print(f"仓库根：{repo}")
    print(f"全量文件所有权矩阵（{len(ownership)} 个子任务）")
    for task_id in sorted(ownership):
        print(f"  {task_id:<6} {'；'.join(ownership[task_id])}")
    print()
    all_conflicts = find_conflicts(ownership, list(ownership))
    print(f"现存重叠对：{len(all_conflicts)}")
    for conflict in all_conflicts:
        print(f"  {conflict['a']} ↔ {conflict['b']}: {'；'.join(conflict['files'])}")


def cross_plan_agent_clashes(plan: Path) -> bool:
    """车道 agent ID 不得被其它 active 计划的车道占用（一个 ID = 一个会话）；有撞车返回 True。"""
    import plans as plans_mod
    try:
        repo = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                                   text=True, cwd=plan.resolve().parent).stdout.strip())
    except OSError:
        return False
    root = repo / ".agentic"
    if not root.is_dir():
        return False
    own = next((name for name, _, meta in plans_mod.active_plans(root)
                if meta.get("source") and (repo / meta["source"]).resolve() == plan.resolve()), "")
    clashes = plans_mod.agent_clashes(root, own, plan)
    for agent, others in clashes.items():
        print(f"✗ 车道 agent ID {agent} 已在 active 计划 {', '.join(others)} 的车道表里（一个 ID 对应一个会话）；"
              f"改用新 ID，或确是同一会话时在本计划 plan.json 的 shared_agents 登记", file=sys.stderr)
    return bool(clashes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="分派前校验任务集合的文件所有权重叠（解析计划文档的「改动文件」列）。"
    )
    parser.add_argument("task_ids", nargs="*", help="准备同波并行的任务 ID，如 P1-1 P2-1 P3-1")
    parser.add_argument("--plan", type=Path, default=None, help="计划文档路径（默认 ./PLAN.md）")
    parser.add_argument("--repo", type=Path, default=None, help="仓库根；默认从 plan 文件位置向上查找 .git")
    parser.add_argument("--header", default=DEFAULT_HEADER, help=f"「改动文件」列标题（默认 {DEFAULT_HEADER}）")
    parser.add_argument("--id-pattern", default=DEFAULT_ID_PATTERN, help=f"任务 ID 正则（默认 {DEFAULT_ID_PATTERN}）")
    parser.add_argument("--all", action="store_true", help="打印全量矩阵与所有现存重叠对（不判错）")
    parser.add_argument("--explain", metavar="ID", help="打印某个任务将改动的文件")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出冲突结果")
    parser.add_argument("--lanes", action="store_true", help="校验车道表与同步点（跨车道共享文件必须已排序）")
    parser.add_argument("--components", action="store_true", help="打印按文件重叠合并的链组")
    args = parser.parse_args(argv)

    plan = args.plan if args.plan is not None else DEFAULT_PLAN
    if args.plan is None and args.repo is not None:
        plan = args.repo / "PLAN.md"
    repo = args.repo if args.repo is not None else infer_repo_root(plan.parent)

    try:
        id_re = re.compile(args.id_pattern)
    except re.error as exc:
        print(f"任务 ID 正则非法：{args.id_pattern}（{exc}）", file=sys.stderr)
        return 2

    if not plan.is_file():
        print(f"找不到计划文档：{plan}", file=sys.stderr)
        return 2

    ownership = parse_plan(plan, args.header, id_re)
    if not ownership:
        print(f"未能从 {plan} 解析出所有权矩阵（列标题：{args.header}）", file=sys.stderr)
        return 2

    if args.lanes or args.components:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import lanes as lanes_mod
        forwarded = ["--plan", str(plan)] + (["--components"] if args.components else [])
        code = lanes_mod.main(forwarded)
        if args.lanes and cross_plan_agent_clashes(Path(plan)):
            return code or 1
        return code

    if args.explain:
        files = ownership.get(args.explain)
        if files is None:
            print(f"未知任务 ID：{args.explain}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps({args.explain: files}, ensure_ascii=False, indent=2))
        else:
            print(f"{args.explain}: {'；'.join(files)}")
        return 0

    if args.all or not args.task_ids:
        if args.json:
            conflicts = find_conflicts(ownership, list(ownership))
            print(json.dumps(conflicts, ensure_ascii=False, indent=2))
        else:
            print_all(ownership, repo)
        return 0

    unknown = [tid for tid in args.task_ids if tid not in ownership]
    if unknown:
        print(f"未知任务 ID：{', '.join(unknown)}", file=sys.stderr)
        return 2

    duplicates = {tid for tid in args.task_ids if args.task_ids.count(tid) > 1}
    if duplicates:
        print(f"任务 ID 重复：{', '.join(sorted(duplicates))}", file=sys.stderr)
        return 2

    conflicts = find_conflicts(ownership, args.task_ids)
    if args.json:
        print(json.dumps(conflicts, ensure_ascii=False, indent=2))
    elif conflicts:
        print(f"✗ 文件所有权冲突：{len(args.task_ids)} 个任务里发现 {len(conflicts)} 对重叠")
        for conflict in conflicts:
            print(f"  {conflict['a']} ↔ {conflict['b']}: {'；'.join(conflict['files'])}")
        print("→ 这些任务必须串行或归到同一个 agent，不得并行。")
    else:
        print(f"✓ 无重叠：{'、'.join(sorted(args.task_ids))} 可同波并行（{len(args.task_ids)} 个任务）")
    return 1 if conflicts else 0


if __name__ == "__main__":
    raise SystemExit(main())
