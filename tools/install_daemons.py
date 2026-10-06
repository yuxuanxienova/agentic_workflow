#!/usr/bin/env python3
"""按 .agentic/config.json 生成并加载本项目的 launchd 守护进程（macOS）。

  bus-wake      bus_wake.py watch --interval 60（唤醒 / 验收 agent / 车道收工）
  lane-patrol   lane_patrol.py --stale 7，每 180 秒
  token-ledger  token_usage.py，每 1800 秒（可选，--with-ledger）

标签 = <launchd_prefix>.<名字>，路径全部由仓库根推导，换项目不用改代码。
  python3 install_daemons.py [--dry-run] [--with-ledger]   写 ~/Library/LaunchAgents 并 load
  python3 install_daemons.py --uninstall                    unload 并删除
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bus import default_root  # noqa: E402
import project_config  # noqa: E402

TOOLS = Path(__file__).resolve().parent
AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"


def specs(root: Path, with_ledger: bool) -> list[tuple[str, list[str], int | None, bool]]:
    """(名字, 参数, StartInterval 秒或 None, KeepAlive)"""
    r = str(root)
    out = [
        ("bus-wake", [str(TOOLS / "bus_wake.py"), "--root", r, "watch", "--interval", "60"], None, True),
        ("lane-patrol", [str(TOOLS / "lane_patrol.py"), "--root", r, "--stale", "7"], 180, False),
    ]
    if with_ledger:
        out.append(("token-ledger", [str(TOOLS / "token_usage.py"), "--root", r], 1800, False))
    return out


def plist(label: str, args: list[str], interval: int | None, keep_alive: bool, repo: Path, log: Path) -> str:
    argv = "".join(f"\n    <string>{escape(a)}</string>" for a in ["/usr/bin/python3", *args])
    sched = (f"\n  <key>StartInterval</key><integer>{interval}</integer>" if interval else "")
    alive = "\n  <key>KeepAlive</key><true/>" if keep_alive else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{escape(label)}</string>
  <key>ProgramArguments</key>
  <array>{argv}
  </array>
  <key>WorkingDirectory</key><string>{escape(str(repo))}</string>
  <key>RunAtLoad</key><true/>{alive}{sched}
  <key>StandardOutPath</key><string>{escape(str(log))}</string>
  <key>StandardErrorPath</key><string>{escape(str(log))}</string>
</dict>
</plist>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成并加载本项目的 launchd 守护进程")
    parser.add_argument("--root", type=Path, default=None, help="协调目录（默认 <git 根>/.agentic）")
    parser.add_argument("--with-ledger", action="store_true", help="同时装 token-ledger")
    parser.add_argument("--dry-run", action="store_true", help="只打印 plist，不写不加载")
    parser.add_argument("--uninstall", action="store_true", help="卸载本项目的守护进程")
    args = parser.parse_args(argv)
    root = (args.root or default_root()).resolve()
    repo = root.parent
    cfg = project_config.load(root)
    logs = root / "bus" / "wake_logs"
    uid = os.getuid()
    for name, params, interval, keep in specs(root, args.with_ledger or args.uninstall):
        label = f"{cfg['launchd_prefix']}.{name}"
        path = AGENTS_DIR / f"{label}.plist"
        if args.uninstall:
            if path.exists():
                subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)], capture_output=True)
                path.unlink()
                print(f"已卸载 {label}")
            continue
        text = plist(label, params, interval, keep, repo, logs / f"{name}.out")
        if args.dry_run:
            print(f"# {path}\n{text}")
            continue
        logs.mkdir(parents=True, exist_ok=True)
        AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)], capture_output=True)
        path.write_text(text, encoding="utf-8")
        proc = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], capture_output=True, text=True)
        print(f"{'已加载' if proc.returncode == 0 else '加载失败'} {label}" +
              (f"：{proc.stderr.strip()}" if proc.returncode else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
