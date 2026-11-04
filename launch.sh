#!/usr/bin/env bash
# 灵枢 SpiritScout 启动器
#   ./launch.sh                      前台启动（Ctrl+C 停止）；额外参数透传给程序（如 --port 8000）
#   ./launch.sh start --offline      启动独立 Ollama 轻量控制台（默认端口 8001）
#   ./launch.sh stop    [端口] [--force]        停止控制台（默认端口 8000；--force = 放弃运行中会话现场）
#   ./launch.sh restart [端口] [--force] [参数]  先停再起；显式给了端口会一并转给新进程，其余参数透传
#   ./launch.sh -h                   启动器用法说明（程序自身参数：./launch.sh start -h）
set -euo pipefail
cd "$(dirname "$0")"
shopt -s nullglob

OFFLINE=""
args=()
for a in "$@"; do
  if [ "$a" = "--offline" ]; then OFFLINE="1"; else args+=("$a"); fi
done
if [ "${#args[@]}" -gt 0 ]; then set -- "${args[@]}"; else set --; fi

DEFAULT_PORT=8000
SESSION_ROOT=.spiritscout
if [ -n "$OFFLINE" ]; then
  DEFAULT_PORT=8001
  SESSION_ROOT=.spiritscout-ollama
fi
PORT="${WEBCONSOLE_PORT:-$DEFAULT_PORT}"

start_console() {
  if [ -n "$OFFLINE" ]; then
    exec bash scripts/run_ollama_light.sh "$@"
  fi
  exec .venv/bin/python -m spiritscout --config configs/system.yaml "$@"
}

case "${1:-start}" in
  help|-h|--help|stop) ;;
  *)
    if [ ! -x .venv/bin/python ]; then
      echo "缺少虚拟环境：先执行 python3 -m venv .venv，再执行 .venv/bin/python -m pip install -e ." >&2
      exit 1
    fi
    ;;
esac

# macOS 没有 ss / GNU grep -P，使用系统 lsof；不把未校验端口拼入匹配式。
listening_pid() {
  local port="$1"
  if ! [[ "$port" =~ ^[0-9]{1,5}$ ]] || [ "$((10#$port))" -lt 1 ] || [ "$((10#$port))" -gt 65535 ]; then
    echo "端口必须为 1–65535" >&2
    return 1
  fi
  if command -v ss >/dev/null 2>&1; then
    ss -tlnp 2>/dev/null | grep -E ":${port}[[:space:]]" \
      | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | head -1 || true
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -1 || true
  else
    echo "找不到 ss 或 lsof，无法定位监听进程；请使用 Ctrl+C 停止前台控制台" >&2
    return 1
  fi
}

# 停止控制台：按端口找 PID，先礼（SIGTERM 等 lifespan 收尾）后兵（超时强杀）
stop_console() {
  local cmd="$1" port="$2" force="$3"
  # 活跃会话保护：有运行中的会话时不掐现场，确认放弃才停
  local running=0 m
  for m in "$SESSION_ROOT"/sessions/*/meta.json; do
    grep -q '"status": *"running"' "$m" 2>/dev/null && running=$((running + 1))
  done
  if [ "$running" -gt 0 ] && [ -z "$force" ]; then
    local mode_flag=""
    [ -n "$OFFLINE" ] && mode_flag=" --offline"
    echo "有 ${running} 个会话正在运行。确认放弃现场请用：./launch.sh ${cmd}${mode_flag} ${port} --force" >&2
    return 1
  fi
  local pid
  # || true：端口空闲时 grep 无匹配会以非零收尾，set -e 会静默退出
  pid=$(listening_pid "$port")
  if [ -z "$pid" ]; then
    echo "控制台未在运行（端口 ${port}）"
    return 0
  fi
  kill "$pid" 2>/dev/null || true
  local i
  for i in $(seq 1 100); do          # 优雅退出最多等 10 秒（关会话/资源助手要时间）
    kill -0 "$pid" 2>/dev/null || { echo "已停止（PID $pid，端口 ${port}）"; return 0; }
    sleep 0.1
  done
  echo "10 秒未退出，强制结束（PID $pid）" >&2
  kill -9 "$pid" 2>/dev/null || true
  sleep 0.3
  if kill -0 "$pid" 2>/dev/null; then
    echo "无法结束 PID $pid，请手动处理" >&2
    return 1
  fi
  echo "已强制停止（PID $pid，端口 ${port}）"
}

case "${1:-start}" in
  -h|--help|help)
    cat <<'USAGE'
灵枢 SpiritScout 启动器

用法：
  ./launch.sh                        前台启动（Ctrl+C 停止）；额外参数透传给程序（如 --port 8000）
  ./launch.sh start [参数...]        同上（显式 start）
  ./launch.sh start --offline        启动独立 Ollama 轻量控制台（默认端口 8001）
  ./launch.sh stop     [端口] [--force]         停止控制台（默认端口 8000，或环境变量 WEBCONSOLE_PORT）
                                                --force = 有运行中会话时确认放弃现场
  ./launch.sh restart  [端口] [--force] [参数]  先停再起；显式端口会转成 --port 传给新进程，其余参数透传
  ./launch.sh stop --offline [端口] [--force]  停止轻量控制台（默认端口 8001）
  ./launch.sh restart --offline [端口] [--force] [参数]  重启轻量控制台，检查其独立会话目录
  ./launch.sh -h | --help | help     本说明

示例：
  ./launch.sh start --offline --port 8101  # 轻量控制台改用 8101
  ./launch.sh stop --offline         # 停轻量控制台（默认端口 8001）
  ./launch.sh stop                   # 停默认端口 8000
  ./launch.sh restart 8100 --force   # 放弃会话现场，重启在 8100
  ./launch.sh --host 0.0.0.0         # 透传给程序前台启动

--offline：选择 .spiritscout-ollama/ 独立配置；首次启动复用本机已下载模型初始化，
不安装依赖、不下载模型、不修改正常配置，也不隔离系统网络或重启 Ollama。
停止行为：按端口找 PID，SIGTERM 优雅退出（最多等 10 秒），超时 SIGKILL。
程序自身参数说明：./launch.sh start -h
USAGE
    ;;
  stop)
    force="" port=""
    for a in "${@:2}"; do
      if [ "$a" = "--force" ]; then force="1"; else port="$a"; fi
    done
    stop_console stop "${port:-$PORT}" "$force"
    ;;
  restart)
    force="" port="" extra=()
    for a in "${@:2}"; do
      if [ "$a" = "--force" ]; then force="1"
      elif [ -z "$port" ] && [[ "$a" != -* ]]; then port="$a"
      else extra+=("$a"); fi
    done
    stop_console restart "${port:-$PORT}" "$force"
    for i in $(seq 1 50); do          # 端口释放要片刻：确认空闲再起，避免撞地址
      [ -n "$(listening_pid "${port:-$PORT}")" ] || break
      sleep 0.1
    done
    args=()
    [ -n "$port" ] && args+=(--port "$port")
    echo "重新启动中（端口 ${port:-默认}）…"
    start_console "${args[@]}" "${extra[@]}"
    ;;
  start)
    start_console "${@:2}"
    ;;
  *)
    start_console "$@"
    ;;
esac
