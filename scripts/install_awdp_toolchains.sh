#!/usr/bin/env bash
# 在 Fedora 44 源码部署机准备 AWDP 工具链；联网准备、断网验收分开执行。
set -euo pipefail

usage() {
    cat <<'EOF'
用法：bash scripts/install_awdp_toolchains.sh [--plan|--install|--packages]

默认 --plan：使用本地 DNF 元数据显示事务与空间，不安装。
--install：通过发行版仓库补齐工具链，需要在本机终端用 sudo 执行。
--packages：打印包清单，不访问仓库。

适用于 Fedora 44 的源码部署。只安装缺失的显式包，依赖可能随之更新；
不执行全系统升级，不添加第三方仓库，不启动服务，不修改模型或用户配置。
TypeScript/Gradle 单独准备；题目框架、指定旧版本及第三方依赖须另行缓存。
EOF
}

action=${1:---plan}
if (( $# > 1 )); then usage >&2; exit 2; fi
case "$action" in
    --help|-h) usage; exit 0 ;;
    --plan|--install|--packages) ;;
    *) usage >&2; exit 2 ;;
esac

packages=(
    gcc gcc-c++ glibc-devel libstdc++-devel make cmake ninja-build pkgconf-pkg-config
    autoconf automake bison flex openssl-devel zlib-ng-compat-devel libffi-devel
    python3 python3-devel python3-pip nodejs22 nodejs22-npm
    java-25-openjdk-devel maven golang
    php-cli php-mbstring php-xml php-pdo php-mysqlnd php-gd php-process php-pecl-zip composer
    rust cargo dotnet-sdk-10.0 mono-devel
    ruby ruby-devel rubygem-bundler perl-App-cpanminus
    lua lua-devel luarocks sqlite git tar unzip zip
)
if [[ "$action" == --packages ]]; then printf '%s\n' "${packages[@]}"; exit 0; fi

source /etc/os-release
if [[ "$ID" != fedora || "$VERSION_ID" != 44 ]]; then
    printf '此包清单仅针对 Fedora 44；当前为 %s，请按实际发行版准备包名。\n' "$PRETTY_NAME" >&2
    exit 2
fi

missing=()
for package in "${packages[@]}"; do
    rpm -q --quiet "$package" || missing+=("$package")
done
if (( ${#missing[@]} == 0 )); then
    printf '系统包已齐备；仍需运行 verify_awdp_environment.py 验证真实沙箱。\n'
    exit 0
fi
printf '待补齐的显式包：\n'
printf '  %s\n' "${missing[@]}"
if [[ "$action" == --plan ]]; then
    exec dnf --cacheonly --assumeno --setopt=install_weak_deps=False install "${missing[@]}"
fi
if (( EUID != 0 )); then
    printf '需要系统包安装权限；请在本机终端运行 sudo bash %q --install。不要发送密码。\n' "$0" >&2
    exit 1
fi
exec dnf --assumeyes --setopt=install_weak_deps=False install "${missing[@]}"
