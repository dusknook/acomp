#!/usr/bin/env bash
# acomp 一键演示（macOS / Linux）。
# 依赖缺失时 demo.py 会自动建 .venv 并安装，无需预先配置。
set -u
cd "$(dirname "$0")"

PY=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done

if [ -z "$PY" ]; then
    echo
    echo "找不到 Python。请先安装 Python 3.10+（macOS: brew install python3）。"
    echo
    exit 1
fi

"$PY" demo.py "$@"
CODE=$?
echo
if [ "$CODE" -eq 0 ]; then echo "演示完成。"; else echo "演示中断，退出码 $CODE。"; fi
exit "$CODE"
