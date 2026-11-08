#!/usr/bin/env bash
# 源码安装：准备项目虚拟环境、核心依赖和缺失配置，保留已有数据。
set -euo pipefail
cd "$(dirname "$0")"

fail() {
  echo "[安装失败] $*" >&2
  exit 1
}

case "${1:-}" in
  -h|--help)
    cat <<'HELP'
用法：./install.sh
创建或复用 .venv，安装核心依赖并检查兼容性，只创建缺失的配置文件。
已有配置、.env 和运行数据保持不变；安装后运行 ./launch.sh。
Linux/WSL2 默认使用 /usr/bin/python3；macOS 使用 PATH 中的 python3。
新建虚拟环境时可指定：SPIRITSCOUT_PYTHON=/usr/bin/python3.12 ./install.sh
HELP
    exit 0
    ;;
esac
if [[ $# -ne 0 ]]; then
  fail "不支持参数；用 ./install.sh --help 查看用法。"
fi

install_system=$(uname -s)
case "$install_system" in
  Linux) install_python=${SPIRITSCOUT_PYTHON:-/usr/bin/python3} ;;
  Darwin) install_python=${SPIRITSCOUT_PYTHON:-python3} ;;
  *) fail "请在 Linux、WSL2 或 macOS 终端运行；Windows 完整部署请进入 WSL2。" ;;
esac

check_python() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' ||
    fail "需要可用的 Python 3.11+：$1。新建环境可用 SPIRITSCOUT_PYTHON 指定解释器。"
}

echo "[1/3] 准备虚拟环境"
if [[ -e .venv || -L .venv ]]; then
  [[ -f .venv/pyvenv.cfg && -x .venv/bin/python ]] ||
    fail "现有 .venv 不是可用的 Linux/macOS 虚拟环境。请先将它移到备份目录，再重试。"
  echo "复用已有 .venv"
else
  check_python "$install_python"
  "$install_python" -m venv .venv ||
    fail "创建 .venv 失败。请按上方错误补齐对应 Python 的 venv/ensurepip 组件，再重试；Debian/Ubuntu 通常需安装 python3-venv。"
fi

check_python .venv/bin/python
.venv/bin/python -c 'import sys; from pathlib import Path; sys.exit(sys.prefix == sys.base_prefix or Path(sys.prefix).resolve() != Path(".venv").resolve())' ||
  fail ".venv/bin/python 没有使用项目虚拟环境。请先备份并修复 .venv，再重试。"
if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
  .venv/bin/python -m ensurepip --upgrade ||
    fail "无法为 .venv 安装 pip。请补齐对应 Python 的 venv/ensurepip 组件，再重试。"
fi

echo "[2/3] 安装并检查核心依赖"
.venv/bin/python -m pip install -e . ||
  fail "依赖安装失败。按上方 pip 错误修复后，重新运行 ./install.sh。"
.venv/bin/python -m pip check ||
  fail "依赖检查失败。按上方冲突信息修复后，重新运行 ./install.sh。"

echo "[3/3] 准备配置"
.venv/bin/python - <<'PY' || fail "配置初始化失败，请检查上方错误；已有配置不会被覆盖。"
from pathlib import Path

configs = Path("configs")
configs.mkdir(exist_ok=True)
for name in ("system.yaml", "models.yaml"):
    target = configs / name
    if target.exists() or target.is_symlink():
        if not target.is_file():
            raise SystemExit(f"配置路径不是有效文件：{target}")
        print(f"保留 {target}")
        continue
    content = ((configs / "system.example.yaml").read_text(encoding="utf-8")
               if name == "system.yaml" else "providers: []\nmodels: []\n")
    try:
        with target.open("x", encoding="utf-8") as output:
            output.write(content)
    except FileExistsError:
        raise SystemExit(f"配置路径已被其他进程创建：{target}，请检查后重试。")
    print(f"创建 {target}")
PY

if [[ "$install_system" == Linux ]] && ! command -v bwrap >/dev/null 2>&1; then
  echo "[提示] 未找到 bubblewrap，shell/Python 工具暂不可用。Debian/Ubuntu：sudo apt install bubblewrap；Fedora：sudo dnf install bubblewrap。"
elif [[ "$install_system" == Darwin ]]; then
  echo "[提示] macOS 原生可运行控制台；shell/Python 工具沙箱需要 Linux。"
fi
echo "核心依赖和配置已准备好。启动：./launch.sh"
