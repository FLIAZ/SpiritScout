#!/usr/bin/env bash
# 构建免安装 Linux ELF 控制台（PyInstaller 单文件）。
# 用法：./scripts/build-elf.sh；可用 BUILD_PYTHON=3.11、BUILD_VENV=.venv-build 覆盖。
# 有 uv 时自动获取指定 Python；否则使用 PYTHON 或 python3（需 3.11–3.14）。
# 产物：dist/spiritscout；首次运行会在可执行文件旁生成 configs/ 与 .spiritscout/。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
BUILD_PYTHON="${BUILD_PYTHON:-3.11}"
VENV="${BUILD_VENV:-.venv-build}"
VENV_PYTHON="$VENV/bin/python"

compatible_python() {
  "$1" -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 15) else 1)' \
    >/dev/null 2>&1
}

if [ -x "$VENV_PYTHON" ] && compatible_python "$VENV_PYTHON"; then
  :
elif command -v uv >/dev/null 2>&1; then
  uv venv --python "$BUILD_PYTHON" --allow-existing "$VENV"
else
  PYTHON_BIN="${PYTHON:-python3}"
  if ! compatible_python "$PYTHON_BIN"; then
    echo "构建需要 Python 3.11–3.14（LiteLLM 1.102.0 暂不支持 3.15）：$("$PYTHON_BIN" --version 2>&1)；请安装 uv 后重试" >&2
    exit 1
  fi
  "$PYTHON_BIN" -m venv --clear "$VENV"
fi

if [ ! -x "$VENV_PYTHON" ] || ! compatible_python "$VENV_PYTHON"; then
  echo "构建环境 Python 不兼容（需 3.11–3.14）；请安装 uv 后重试" >&2
  exit 1
fi

if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$VENV_PYTHON" -e '.[build]'
else
  "$VENV_PYTHON" -m pip install --upgrade pip
  "$VENV_PYTHON" -m pip install -e '.[build]'
fi

"$VENV_PYTHON" -m PyInstaller --clean --noconfirm scripts/spiritscout.spec
./dist/spiritscout --version
echo "构建完成：$ROOT/dist/spiritscout"
