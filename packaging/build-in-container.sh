#!/usr/bin/env bash
# /source 是白名单快照；/output 是独立构建目录，不挂载用户配置或 Docker socket。
set -euo pipefail
export LITELLM_LOCAL_MODEL_COST_MAP=True
export PIP_DISABLE_PIP_VERSION_CHECK=1
export TIKTOKEN_CACHE_DIR=/work/tiktoken-cache
mkdir -p /work /output "$TIKTOKEN_CACHE_DIR"
rm -rf /work/source
cp -a /source /work/source
/usr/local/bin/python -c 'import sysconfig,platform; assert sysconfig.get_config_var("Py_ENABLE_SHARED"); assert tuple(map(int,platform.libc_ver()[1].split(".")[:2])) <= (2,31), "构建镜像 glibc 超出 2.31 基线"'
sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list
apt-get -o Acquire::Retries=1 -o Acquire::https::Timeout=20 update -qq
apt-get install -y --no-install-recommends binutils
/usr/local/bin/python -m venv /work/venv
/work/venv/bin/python -m pip install 'pyinstaller==6.22.0' 'setuptools>=77' wheel
/work/venv/bin/python -m pip install /work/source
cd /work/source
/work/venv/bin/python -m PyInstaller --noconfirm --distpath /output --workpath /work/build packaging/spiritscout.spec
/output/spiritscout --version
/work/venv/bin/python -m pip freeze > /output/build-requirements.txt
