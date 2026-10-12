#!/usr/bin/env bash
# 灵探 SpiritScout 启动器
#   ./launch.sh            前台启动（Ctrl+C 停止）；额外参数透传给程序（如 --port 8000）
#   ./launch.sh stop [端口]  停止控制台（默认端口 8000）
set -euo pipefail
cd "$(dirname "$0")"

PORT="${WEBCONSOLE_PORT:-8000}"

if [ ! -x .venv/bin/python ]; then
  echo "缺少虚拟环境：先执行 python3 -m venv .venv && .venv/bin/pip install -e '.[toolkit]'" >&2
  exit 1
fi

case "${1:-start}" in
  stop)
    force=""
    port="$PORT"
    for a in "${@:2}"; do
      if [ "$a" = "--force" ]; then force="1"; else port="$a"; fi
    done
    # 活跃会话保护：有运行中的会话时不掐现场，确认放弃才停
    running=0
    for m in .spiritscout/sessions/*/meta.json; do
      grep -q '"status": *"running"' "$m" 2>/dev/null && running=$((running + 1))
    done
    if [ "$running" -gt 0 ] && [ -z "$force" ]; then
      echo "有 ${running} 个会话正在运行。确认放弃现场请用：./launch.sh stop --force" >&2
      exit 1
    fi
    pid=$(ss -tlnp 2>/dev/null | grep ":${port}" | grep -oP 'pid=\K[0-9]+' | head -1)
    if [ -n "$pid" ]; then
      kill "$pid" && echo "已停止（PID $pid，端口 ${port}）"
    else
      echo "控制台未在运行（端口 ${port}）"
    fi
    ;;
  start)
    exec .venv/bin/python -m spiritscout --config configs/system.yaml "${@:2}"
    ;;
  *)
    exec .venv/bin/python -m spiritscout --config configs/system.yaml "$@"
    ;;
esac
