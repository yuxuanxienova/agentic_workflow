#!/usr/bin/env python3
"""校验功能域树 JSON（通用 Agentic Workflow 版）。

域树把「大而模糊的目标」拆成功能域 → 子域 → …的树，叶子节点即最小可并行的
工作单元。结构（templates/ 与 examples/ 共用此结构）：

  {
    "version": 1,
    "project": "<项目名>",
    "roots": [
      {
        "id": "battle", "name": "战斗/战术", "level": 1,
        "surface": "<产品面：页面/流程，可空>",
        "code": ["Godot/app/tactical"],
        "children": [
          {
            "id": "battle.radar", "name": "雷达", "level": 2,
            "files": ["Godot/presentation/tactical_hud/battle_radar_view.gd"],
            "acceptance": "<唯一一条验收标准>",
            "leaf": true,
            "children": []
          }
        ]
      }
    ]
  }

校验规则（违反即退出 1 并逐条列出）：
  1. 必须存在 version 字段。
  2. 每个节点 id 全局唯一且非空。
  3. level 与嵌套深度一致（root=1）。
  4. 最大深度 ≤4。
  5. leaf 为 true 的节点须满足停止规则：files 数量 ≤3 且 acceptance 非空
     （若该叶子没有 files 字段则跳过文件数检查，但仍需 acceptance）。
  6. leaf 为假或缺失的节点必须有非空 children。
  7. 同一父节点下的多个兄弟叶子不得出现相同文件路径（那说明该父节点切分错误）。

用法：
  python3 tools/validate_domains.py domains.json
  python3 tools/validate_domains.py domains.json --json

退出码：0 通过；1 存在校验违规；2 用法或数据错误（文件缺失/JSON 非法）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MAX_DEPTH = 4
MAX_LEAF_FILES = 3


def describe(node: dict | None, fallback: str) -> str:
    """给节点一个便于定位的标签。"""
    if isinstance(node, dict) and node.get("id"):
        return str(node["id"])
    return fallback


def check_sibling_duplicates(node: dict, errors: list[str]) -> None:
    """检查同一父节点下的兄弟叶子是否重复声明了同一文件。"""
    file_to_leaves: dict[str, list[str]] = {}
    for child in node.get("children") or []:
        if not isinstance(child, dict) or not child.get("leaf"):
            continue
        for path in child.get("files") or []:
            file_to_leaves.setdefault(path, []).append(describe(child, "<无 id 叶子>"))
    for path, leaves in file_to_leaves.items():
        if len(leaves) > 1:
            errors.append(f"兄弟叶子重复声明文件 {path}：{', '.join(leaves)}（父 {describe(node, '<根>')} 切分错误）")


def walk(node: dict, depth: int, errors: list[str], seen: dict[str, str]) -> None:
    """递归校验单个节点及其子树。"""
    if not isinstance(node, dict):
        errors.append(f"深度 {depth} 存在非对象节点")
        return

    node_id = node.get("id")
    if not node_id or not str(node_id).strip():
        errors.append(f"深度 {depth} 的节点缺少非空 id")
    elif node_id in seen:
        errors.append(f"节点 id 重复：{node_id}（首次出现于深度 {seen[node_id]}，再次出现于深度 {depth}）")
    else:
        seen[node_id] = str(depth)

    level = node.get("level")
    if level != depth:
        errors.append(f"节点 {describe(node, '<无 id>')} 的 level={level} 与嵌套深度 {depth} 不一致（应为 {depth}）")

    if depth > MAX_DEPTH:
        errors.append(f"节点 {describe(node, '<无 id>')} 深度 {depth} 超过最大深度 {MAX_DEPTH}")

    leaf = bool(node.get("leaf"))
    children = node.get("children") or []
    if leaf:
        acceptance = node.get("acceptance")
        if not acceptance or not str(acceptance).strip():
            errors.append(f"叶子 {describe(node, '<无 id>')} 缺少非空 acceptance")
        if "files" in node:
            files = node.get("files") or []
            if len(files) > MAX_LEAF_FILES:
                errors.append(
                    f"叶子 {describe(node, '<无 id>')} 的 files 数量 {len(files)} 超过上限 {MAX_LEAF_FILES}（停止规则未满足）"
                )
    else:
        if not children:
            errors.append(f"非叶子节点 {describe(node, '<无 id>')} 必须有非空 children")

    check_sibling_duplicates(node, errors)

    for child in children:
        walk(child, depth + 1, errors, seen)


def validate(tree: object) -> list[str]:
    """校验整棵域树，返回违规清单（空表示通过）。"""
    errors: list[str] = []
    if not isinstance(tree, dict):
        return ["根节点必须是 JSON 对象"]
    if "version" not in tree:
        errors.append("缺少 version 字段")

    roots = tree.get("roots")
    if not isinstance(roots, list) or not roots:
        errors.append("roots 必须是非空数组")
        return errors

    seen: dict[str, str] = {}
    # 用伪根检查 roots 之间的兄弟重复。
    check_sibling_duplicates({"children": roots}, errors)
    for root in roots:
        walk(root, 1, errors, seen)
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验功能域树 JSON：id 唯一、level 一致、深度 ≤4、叶子停规则、兄弟文件不重复。")
    parser.add_argument("domains", type=Path, help="功能域树 JSON 路径")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出校验结果")
    args = parser.parse_args(argv)

    if not args.domains.is_file():
        print(f"找不到域树文件：{args.domains}", file=sys.stderr)
        return 2
    try:
        tree = json.loads(args.domains.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"JSON 解析失败：{exc}", file=sys.stderr)
        return 2

    errors = validate(tree)
    if args.json:
        print(json.dumps({"ok": not errors, "errors": errors}, ensure_ascii=False, indent=2))
    elif errors:
        print(f"✗ 域树校验失败：{len(errors)} 处违规")
        for index, message in enumerate(errors, 1):
            print(f"  {index}. {message}")
    else:
        print(f"✓ 域树校验通过：{args.domains}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
