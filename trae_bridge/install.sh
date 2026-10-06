#!/usr/bin/env bash
# 把总线桥扩展装进 Trae CN（直接写扩展目录 + 登记 extensions.json），装完在 Trae 里 Developer: Reload Window。
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
EXT="$HOME/.trae-cn/extensions"
DST="$EXT/local.agentic-bus-bridge-0.0.1"
mkdir -p "$DST" && cp "$SRC/package.json" "$SRC/extension.js" "$DST/"
python3 - "$EXT/extensions.json" "$DST" <<'P'
import json, os, sys, time
path, dst = sys.argv[1], sys.argv[2]
data = json.load(open(path)) if os.path.exists(path) else []
data = [x for x in data if x["identifier"]["id"] != "local.agentic-bus-bridge"]
data.append({"identifier": {"id": "local.agentic-bus-bridge"}, "version": "0.0.1",
             "location": {"$mid": 1, "path": dst, "scheme": "file"},
             "relativeLocation": os.path.basename(dst),
             "metadata": {"installedTimestamp": int(time.time() * 1000), "source": "vsix"}})
json.dump(data, open(path, "w"))
P
echo "已安装到 $DST；请在 Trae 里执行 Developer: Reload Window"
