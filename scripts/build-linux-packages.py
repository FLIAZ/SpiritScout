#!/usr/bin/env python3
"""构建本机架构 ELF、AppImage、deb、rpm 和 Arch 包；不会发布或安装到宿主。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import platform
import shlex
import shutil
import struct
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHES = {"x86_64": (62, "amd64", "x86_64"), "aarch64": (183, "arm64", "aarch64")}


def run(args, **kwargs):
    print("+", " ".join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def version():
    tree = ast.parse((ROOT / "src/spiritscout/__init__.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise ValueError("找不到唯一版本来源")


def elf_arch(path):
    with Path(path).open("rb") as binary:
        header = binary.read(20)
    if header[:4] != b"\x7fELF" or header[4:6] != b"\x02\x01":
        raise ValueError("需要 64 位小端 ELF")
    machine = struct.unpack_from("<H", header, 18)[0]
    for arch, values in ARCHES.items():
        if machine == values[0]:
            return arch
    raise ValueError(f"不支持的 ELF 架构：{machine}")


def snapshot(destination):
    # 不用 git archive：允许测试当前协作改动，但绝不携带本地配置、数据或缓存。
    for folder in ("src", "packaging"):
        shutil.copytree(
            ROOT / folder,
            destination / folder,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
        )
    for name in ("pyproject.toml", "README.md", "LICENSE", ".env.example"):
        shutil.copy2(ROOT / name, destination / name)
    (destination / "configs").mkdir()
    for path in (ROOT / "configs").glob("*.example.yaml"):
        shutil.copy2(path, destination / "configs" / path.name)


def write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def copy_license(source, destination):
    # Go 模块缓存的许可证通常只读；重新构建时替换旧产物而不修改源缓存。
    destination.unlink(missing_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o644)


def proxy_path(binary):
    return binary.with_name(
        binary.name.replace("spiritscout-linux-", "spiritscout-traffic-proxy-linux-")
    )


def build_proxy(binary, arch):
    target = proxy_path(binary)
    env = dict(
        os.environ,
        CGO_ENABLED="0",
        GOOS="linux",
        GOARCH="amd64" if arch == "x86_64" else "arm64",
        GOTOOLCHAIN="local",
        GOMAXPROCS="2",
    )
    run(
        ["go", "build", "-mod=readonly", "-trimpath", "-o", target, "."],
        cwd=ROOT / "native/traffic-proxy",
        env=env,
    )
    if elf_arch(target) != arch:
        raise ValueError("Go 代理架构错误")
    notices = target.with_name(target.name + "-licenses")
    notices.mkdir(exist_ok=True)
    data = subprocess.check_output(
        [
            "go",
            "list",
            "-deps",
            "-f",
            "{{if .Module}}{{.Module.Path}}|{{.Module.Dir}}{{end}}",
            ".",
        ],
        cwd=ROOT / "native/traffic-proxy",
        env=env,
        text=True,
        timeout=60,
    )
    for row in sorted(set(data.splitlines())):
        if "|" not in row:
            continue
        module, directory_name = row.split("|", 1)
        directory = Path(directory_name)
        for name in ("LICENSE", "LICENSE.txt", "COPYING", "NOTICE"):
            if (directory / name).is_file():
                copy_license(
                    directory / name, notices / (module.replace("/", "_") + "-" + name)
                )
    goroot = subprocess.check_output(
        ["go", "env", "GOROOT"], env=env, text=True
    ).strip()
    license_path = next(
        (
            p
            for p in (
                Path(goroot) / "LICENSE",
                Path("/usr/share/licenses/golang/LICENSE"),
                Path("/usr/share/licenses/golang-bin/LICENSE"),
            )
            if p.is_file()
        ),
        None,
    )
    if license_path is None:
        raise ValueError("未找到 Go 标准库许可证，请使用完整的 Go SDK")
    copy_license(license_path, notices / "Go-LICENSE")
    if license_path.with_name("PATENTS").is_file():
        copy_license(license_path.with_name("PATENTS"), notices / "Go-PATENTS")
    return target


def payload(directory, binary, package_format):
    target = directory / "usr/lib/spiritscout/spiritscout"
    target.parent.mkdir(parents=True)
    shutil.copy2(binary, target)
    target.chmod(0o755)
    proxy = proxy_path(binary)
    shutil.copy2(proxy, target.with_name("spiritscout-traffic-proxy"))
    shutil.copytree(
        proxy.with_name(proxy.name + "-licenses"),
        directory / "usr/share/licenses/spiritscout/traffic-proxy",
    )
    write(
        directory / "usr/bin/spiritscout",
        f"""#!/bin/sh
set -eu
export SPIRITSCOUT_PACKAGE_FORMAT={package_format}
export SPIRITSCOUT_TRAFFIC_PROXY="/usr/lib/spiritscout/spiritscout-traffic-proxy"
export SPIRITSCOUT_DATA_HOME="${{SPIRITSCOUT_DATA_HOME:-${{XDG_DATA_HOME:-$HOME/.local/share}}/spiritscout}}"
exec /usr/lib/spiritscout/spiritscout "$@"
""",
        0o755,
    )
    for relative, source in (
        (
            "usr/share/applications/spiritscout.desktop",
            ROOT / "packaging/linux/spiritscout.desktop",
        ),
        (
            "usr/share/icons/hicolor/scalable/apps/spiritscout.svg",
            ROOT / "packaging/linux/spiritscout.svg",
        ),
        ("usr/share/licenses/spiritscout/LICENSE", ROOT / "LICENSE"),
    ):
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, path)
    write(
        directory / "usr/share/doc/spiritscout/README.txt",
        "SpiritScout 本地开发构建\n启动：spiritscout；默认 http://127.0.0.1:8000\n配置与会话：${XDG_DATA_HOME:-~/.local/share}/spiritscout\n需要 glibc >= 2.31。完整沙箱需 bubblewrap、系统 Python 3.11+；浏览器工具需 Node.js 22+ 与 Chromium。\n未内置外部安全工具或用户凭据。升级使用原包管理器，卸载不删除用户数据。\n",
    )


def package_deb(work, binary, output, arch, ver):
    stage = work / "deb"
    payload(stage, binary, "deb")
    size = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file()) // 1024
    write(
        stage / "DEBIAN/control",
        f"""Package: spiritscout
Version: {ver}-1
Architecture: {ARCHES[arch][1]}
Maintainer: SpiritScout contributors <noreply@github.com>
Section: net
Priority: optional
Installed-Size: {size}
Depends: libc6 (>= 2.31)
Recommends: bubblewrap, python3, nodejs, chromium
Homepage: https://github.com/FLIAZ/SpiritScout
Description: SpiritScout local multi-agent security testing console
 Self-contained Python console; user configuration remains in the user data directory.
""",
    )
    path = output / f"spiritscout_{ver}-1_{ARCHES[arch][1]}.deb"
    run(["dpkg-deb", "--root-owner-group", "--build", stage, path])
    run(["dpkg-deb", "--info", path], stdout=subprocess.DEVNULL)
    return path


def package_rpm(work, binary, output, arch, ver):
    top = work / "rpm"
    for folder in ("BUILD", "BUILDROOT", "RPMS", "SOURCES", "SPECS", "SRPMS"):
        (top / folder).mkdir(parents=True)
    stage = top / "payload"
    payload(stage, binary, "rpm")
    spec = top / "SPECS/spiritscout.spec"
    write(
        spec,
        f"""%global debug_package %{{nil}}
%global __os_install_post %{{nil}}
Name: spiritscout
Version: {ver}
Release: 1
Summary: SpiritScout local multi-agent security testing console
License: Apache-2.0
URL: https://github.com/FLIAZ/SpiritScout
BuildArch: {ARCHES[arch][2]}
AutoReqProv: no
Requires: glibc >= 2.31
Requires: /bin/sh
%description
Self-contained console. Sandbox tools and browser runtimes are installed separately.
%prep
%build
%install
mkdir -p %{{buildroot}}
cp -a {shlex.quote(str(stage))}/usr %{{buildroot}}/
%files
/usr/bin/spiritscout
/usr/lib/spiritscout
/usr/share/applications/spiritscout.desktop
/usr/share/icons/hicolor/scalable/apps/spiritscout.svg
%license /usr/share/licenses/spiritscout
%doc /usr/share/doc/spiritscout/README.txt
""",
    )
    run(
        [
            "rpmbuild",
            "-bb",
            "--target",
            ARCHES[arch][2],
            "--define",
            f"_topdir {top}",
            spec,
        ]
    )
    built = list((top / "RPMS").rglob("*.rpm"))
    if len(built) != 1:
        raise ValueError("RPM 产物数异常")
    path = output / built[0].name
    shutil.copy2(built[0], path)
    run(["rpm", "-qip", path], stdout=subprocess.DEVNULL)
    return path


def package_arch(work, binary, output, arch, ver):
    stage = work / "arch"
    payload(stage, binary, "arch")
    size = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file())
    write(
        stage / ".PKGINFO",
        f"""pkgname = spiritscout
pkgbase = spiritscout
pkgver = {ver}-1
pkgdesc = SpiritScout local multi-agent security testing console
url = https://github.com/FLIAZ/SpiritScout
builddate = {int(os.environ.get("SOURCE_DATE_EPOCH", time.time()))}
packager = SpiritScout contributors
size = {size}
arch = {arch}
license = Apache-2.0
xdata = pkgtype=pkg
depend = glibc>=2.31
optdepend = bubblewrap: sandbox execution
optdepend = python: sandbox Python tools
optdepend = nodejs: browser MCP
optdepend = chromium: browser automation
""",
    )
    # ALPM 原生 tar + Zstandard 格式，所有成员的 uid/gid 固定为 root。
    path = output / f"spiritscout-{ver}-1-{arch}.pkg.tar.zst"
    tar = work / "arch.tar"
    with tarfile.open(tar, "w", format=tarfile.PAX_FORMAT) as archive:
        for entry in sorted(stage.rglob("*")):
            info = archive.gettarinfo(str(entry), entry.relative_to(stage).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            with entry.open("rb") if entry.is_file() else io.BytesIO() as data:
                archive.addfile(info, data if entry.is_file() else None)
    run(["zstd", "-T2", "-q", "-f", tar, "-o", path])
    return path


def download_appimage_runtime(directory, arch):
    """仅下载官方 type-2 runtime；文件系统用宿主 squashfs-tools 构建。"""
    repo, name = "AppImage/type2-runtime", f"runtime-{arch}"
    headers = {"User-Agent": "SpiritScout-package-builder"}
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest", headers=headers
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    asset = next(a for a in release["assets"] if a["name"] == name)
    digest = asset.get("digest", "")
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise ValueError("官方 runtime 缺少可核对的 SHA-256")
    target = directory / name
    if (
        not target.is_file()
        or hashlib.sha256(target.read_bytes()).hexdigest() != digest[7:]
    ):
        request = urllib.request.Request(asset["browser_download_url"], headers=headers)
        with (
            urllib.request.urlopen(request, timeout=60) as response,
            target.open("wb") as output,
        ):
            shutil.copyfileobj(response, output, 65536)
    if hashlib.sha256(target.read_bytes()).hexdigest() != digest[7:]:
        target.unlink()
        raise ValueError("runtime SHA-256 校验失败")
    if elf_arch(target) != arch:
        raise ValueError("runtime 架构与应用架构不符")
    write(
        directory / "appimage-tools.json",
        json.dumps(
            [
                {
                    "repository": repo,
                    "tag": release["tag_name"],
                    "name": name,
                    "digest": digest,
                }
            ],
            indent=2,
        ),
    )
    return target


def package_appimage(work, binary, output, arch, ver, tools):
    appdir = work / "SpiritScout.AppDir"
    target = appdir / "usr/bin/spiritscout"
    target.parent.mkdir(parents=True)
    shutil.copy2(binary, target)
    write(
        appdir / "AppRun",
        """#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export SPIRITSCOUT_PACKAGE_FORMAT=appimage
export SPIRITSCOUT_TRAFFIC_PROXY="$HERE/usr/bin/spiritscout-traffic-proxy"
export SPIRITSCOUT_DATA_HOME="${SPIRITSCOUT_DATA_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/spiritscout}"
exec "$HERE/usr/bin/spiritscout" "$@"
""",
        0o755,
    )
    shutil.copy2(
        ROOT / "packaging/linux/spiritscout.desktop", appdir / "spiritscout.desktop"
    )
    shutil.copy2(ROOT / "packaging/linux/spiritscout.svg", appdir / "spiritscout.svg")
    shutil.copy2(proxy_path(binary), target.with_name("spiritscout-traffic-proxy"))
    shutil.copytree(
        proxy_path(binary).with_name(proxy_path(binary).name + "-licenses"),
        appdir / "usr/share/licenses/spiritscout/traffic-proxy",
    )
    shutil.copy2(ROOT / "LICENSE", appdir / "usr/share/licenses/spiritscout/LICENSE")
    run(["desktop-file-validate", appdir / "spiritscout.desktop"])
    shutil.copy2(
        ROOT / "packaging/linux/appimage-runtime-LICENSE",
        appdir / "usr/share/licenses/spiritscout/appimage-runtime-LICENSE",
    )
    runtime = download_appimage_runtime(tools, arch)
    filesystem = work / "appimage.squashfs"
    run(
        [
            "mksquashfs",
            appdir,
            filesystem,
            "-noappend",
            "-comp",
            "gzip",
            "-all-root",
            "-processors",
            "2",
        ]
    )
    path = output / f"SpiritScout-{ver}-{arch}.AppImage"
    prefix = bytearray(runtime.read_bytes())
    prefix[8:11] = b"AI\x02"  # AppImage type-2 标记位于 ELF 保留字节。
    with path.open("wb") as output_file, filesystem.open("rb") as payload_file:
        output_file.write(prefix)
        shutil.copyfileobj(payload_file, output_file)
    path.chmod(0o755)
    run([path, "--appimage-version"])

    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=ARCHES, default=platform.machine())
    parser.add_argument(
        "--index-url",
        default="https://pypi.org/simple",
        help="公开 HTTPS 构建镜像源，不接受凭据",
    )
    parser.add_argument("--build-image", default="python:3.11-slim-bullseye")
    parser.add_argument("--binary", type=Path, help="使用已经构建且架构匹配的 ELF")
    parser.add_argument("--output", type=Path, default=ROOT / "dist/linux")
    parser.add_argument(
        "--formats",
        nargs="+",
        choices=["deb", "rpm", "arch", "appimage"],
        default=["deb", "rpm", "arch", "appimage"],
    )
    args = parser.parse_args()
    from urllib.parse import urlsplit

    index = urlsplit(args.index_url)
    if (
        index.scheme != "https"
        or index.username
        or index.password
        or not index.hostname
    ):
        parser.error("构建索引必须是无凭据 HTTPS 地址")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    ver = version()
    if args.arch != platform.machine():
        parser.error("请在对应架构构建机运行，不能将本机二进制改名为其他架构")
    with tempfile.TemporaryDirectory(
        prefix="spiritscout-packaging-", dir=output
    ) as temporary:
        work = Path(temporary)
        binary = (
            output
            / f"spiritscout-linux-{'arm64' if args.arch == 'aarch64' else args.arch}"
        )
        if args.binary:
            if elf_arch(args.binary) != args.arch:
                parser.error("ELF 架构与目标不一致")
            if args.binary.resolve() != binary:
                shutil.copy2(args.binary, binary)
        else:
            stage, frozen = work / "source", work / "frozen"
            stage.mkdir()
            frozen.mkdir()
            snapshot(stage)
            image = args.build_image
            run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--cpus=2",
                    "-e",
                    f"PIP_INDEX_URL={args.index_url}",
                    "--memory=3g",
                    "-v",
                    "spiritscout-build-pip-cache:/root/.cache/pip",
                    "-v",
                    f"{stage}:/source:ro",
                    "-v",
                    f"{frozen}:/output",
                    image,
                    "bash",
                    "/source/packaging/build-in-container.sh",
                ]
            )
            shutil.copy2(frozen / "spiritscout", binary)
            shutil.copy2(
                frozen / "build-requirements.txt",
                output / f"build-requirements-{args.arch}.txt",
            )
        if elf_arch(binary) != args.arch:
            raise ValueError("构建产物架构错误")
        run([binary, "--version"])
        proxy = build_proxy(binary, args.arch)
        products = [binary, proxy]
        for fmt in args.formats:
            builder = {
                "deb": package_deb,
                "rpm": package_rpm,
                "arch": package_arch,
                "appimage": package_appimage,
            }[fmt]
            tools = output / ".appimage-tools"
            tools.mkdir(exist_ok=True)
            products.append(
                builder(work, binary, output, args.arch, ver, tools)
                if fmt == "appimage"
                else builder(work, binary, output, args.arch, ver)
            )
            if fmt == "appimage":
                shutil.copy2(
                    tools / "appimage-tools.json",
                    output / f"appimage-tools-{args.arch}.json",
                )
        manifest = {
            "version": ver,
            "architecture": args.arch,
            "minimum_glibc": "2.31",
            "development_build": True,
            "files": [
                {
                    "name": p.name,
                    "size": p.stat().st_size,
                    "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                }
                for p in products
            ],
        }
        write(
            output / f"manifest-{args.arch}.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
        write(
            output / f"SHA256SUMS-{args.arch}",
            "".join(f"{f['sha256']}  {f['name']}\n" for f in manifest["files"]),
        )
        print("产物目录：", output)


if __name__ == "__main__":
    main()
