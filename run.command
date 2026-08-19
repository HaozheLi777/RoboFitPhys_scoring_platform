#!/bin/bash
# macOS 双击启动入口
cd "$(dirname "$0")" || exit 1
if command -v python3 >/dev/null 2>&1; then
  exec python3 run.py
else
  exec python run.py
fi
