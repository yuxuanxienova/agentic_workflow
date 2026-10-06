#!/usr/bin/env python3
"""项目配置：把各项目不同的值集中到 `<root>/config.json`（root 默认 `<git 根>/.agentic`，入库）。

缺省值全部由仓库目录名推导，新仓库不写配置也能跑；写了的键覆盖缺省。字段：

  project          项目短名（小写，用于 agent ID / launchd 标签），缺省 = 仓库目录名小写
  display_name     给人看的名字（验收提示词里用），缺省 = 仓库目录名
  coordinator      协调人 ID 兜底（计划文档里「协调 `<ID>`」优先），缺省 claude-<project>-coord
  reviewer_id      验收 agent ID，缺省 claude-<project>-review
  patrol_id        巡检发送方 ID，缺省 claude-<project>-patrol
  handoff_dir      协调人交接文件目录（仓库相对），缺省 docs/handoffs
  launchd_prefix   launchd 标签前缀，缺省 com.<project>
  review.test_command  验收 agent 在临时 worktree 里跑的测试命令；空 = 只看 diff 与证据，不跑测试
  review.levels        验收等级定义一句话（L0/L1/L2 各指什么）

用法：`python3 project_config.py show` 打印生效配置；`init` 在 root 下写一份带缺省值的 config.json。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

CONFIG_NAME = "config.json"


def defaults(repo: Path) -> dict:
    name = repo.resolve().name
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"
    return {
        "project": slug,
        "display_name": name,
        "coordinator": f"claude-{slug}-coord",
        "reviewer_id": f"claude-{slug}-review",
        "patrol_id": f"claude-{slug}-patrol",
        "handoff_dir": "docs/handoffs",
        "launchd_prefix": f"com.{slug}",
        "review": {
            "test_command": "",
            "levels": "L0 = 单测 / 构建通过；L1 = 真实运行 + 脚本化输入 + 截图与状态断言；L2 = 对照基准的人工结论",
        },
    }


def load(root: Path) -> dict:
    """生效配置 = 缺省值 ← root/config.json（review 子表逐键合并）。"""
    root = Path(root)
    cfg = defaults(root.resolve().parent)
    path = root / CONFIG_NAME
    if path.is_file():
        user = json.loads(path.read_text(encoding="utf-8"))
        review = {**cfg["review"], **(user.pop("review", None) or {})}
        cfg.update(user)
        cfg["review"] = review
        # 只改了 project 时，派生 ID 跟着走
        base = defaults(root.resolve().parent)
        for key, tmpl in (("coordinator", "claude-{}-coord"), ("reviewer_id", "claude-{}-review"),
                          ("patrol_id", "claude-{}-patrol"), ("launchd_prefix", "com.{}")):
            if key not in user and cfg["project"] != base["project"]:
                cfg[key] = tmpl.format(cfg["project"])
    return cfg


def main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from bus import default_root

    parser = argparse.ArgumentParser(description="项目配置（.agentic/config.json）")
    parser.add_argument("--root", type=Path, default=None, help="协调目录（默认 <git 根>/.agentic）")
    parser.add_argument("cmd", choices=["show", "init"])
    args = parser.parse_args(argv)
    root = args.root or default_root()
    if args.cmd == "show":
        print(json.dumps(load(root), ensure_ascii=False, indent=2))
        return 0
    path = root / CONFIG_NAME
    if path.exists():
        print(f"已存在：{path}", file=sys.stderr)
        return 1
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(defaults(root.resolve().parent), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    print(f"已写入 {path}，按项目改 review.test_command / handoff_dir 等")
    return 0


if __name__ == "__main__":
    sys.exit(main())
