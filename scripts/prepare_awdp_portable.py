#!/usr/bin/env python3
"""准备沙箱可见的 TypeScript/Gradle 和 Maven 基础插件缓存；无需 root。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import shlex
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

TYPESCRIPT_VERSION = "7.0.2"
GRADLE_VERSION = "9.9.0"
GRADLE_SHA256 = "0b00b0ffe07c4d4789c12d4935a44a7057d55774b7540bd4a3f76f336deb177a"
WRAPPER_MARKER = "# SpiritScout AWDP portable toolchain."


def wrapper(path: Path, executable: Path) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or WRAPPER_MARKER not in path.read_text():
            raise FileExistsError(f"保留已有命令，请先检查：{path}")
    path.write_text(f'#!/bin/sh\n{WRAPPER_MARKER}\nexec {shlex.quote(str(executable))} "$@"\n')
    path.chmod(0o755)


def prepare(venv: Path) -> dict:
    if os.geteuid() == 0:
        raise RuntimeError("请用项目所属普通用户运行，避免在 venv 内创建 root 文件")
    if not (venv / "bin/python").exists():
        raise RuntimeError(f"缺少已安装的项目虚拟环境：{venv}")
    # 与沙箱已有的 venv 只读挂载共用工具层，不开放主目录或 /usr/local。
    root = venv / "share/awdp-toolchains"
    root.mkdir(parents=True, exist_ok=True)
    npm = shutil.which("npm", path="/usr/bin:/bin")
    if not npm:
        raise RuntimeError("请先在 /usr/bin 安装发行版的 Node.js/npm")
    typescript = root / f"typescript-{TYPESCRIPT_VERSION}"
    tsc = typescript / "node_modules/.bin/tsc"
    if not tsc.exists():
        subprocess.run([
            npm, "install", "--prefix", str(typescript), "--save-exact",
            "--ignore-scripts", "--no-audit", "--no-fund",
            "--cache", str(root / "npm-cache"), f"typescript@{TYPESCRIPT_VERSION}",
        ], check=True, timeout=180, env={**os.environ, "PATH": "/usr/bin:/bin"})
    wrapper(venv / "bin/tsc", tsc)

    gradle = root / f"gradle-{GRADLE_VERSION}"
    if not (gradle / ".verified-sha256").exists():
        if gradle.exists():
            raise FileExistsError(f"保留未验证的目录，请检查后再重试：{gradle}")
        with tempfile.TemporaryDirectory(prefix=".gradle-download-", dir=root) as temp:
            staging = Path(temp)
            archive = staging / "gradle.zip"
            url = f"https://services.gradle.org/distributions/gradle-{GRADLE_VERSION}-bin.zip"
            with urllib.request.urlopen(url, timeout=60) as response, archive.open("wb") as out:
                shutil.copyfileobj(response, out)
            with archive.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            if digest != GRADLE_SHA256:
                raise ValueError("Gradle 官方发行包的 SHA-256 不匹配")
            with zipfile.ZipFile(archive) as bundle:
                for item in bundle.infolist():
                    parts = Path(item.filename).parts
                    if not parts or parts[0] != gradle.name or ".." in parts:
                        raise ValueError("Gradle 压缩包路径异常")
                bundle.extractall(staging)
            extracted = staging / gradle.name
            (extracted / "bin/gradle").chmod(0o755)
            (extracted / ".verified-sha256").write_text(digest + "\n")
            extracted.rename(gradle)
    if (gradle / ".verified-sha256").read_text().strip() != GRADLE_SHA256:
        raise ValueError("已有 Gradle 版本校验记录不匹配")
    wrapper(venv / "bin/gradle", gradle / "bin/gradle")
    maven = shutil.which("mvn", path="/usr/bin:/bin")
    if not maven:
        raise RuntimeError("请先安装发行版 Maven/JDK")
    # 只缓存验收所需的基础插件，不声称涵盖 Spring 等题目依赖。
    spec = runpy.run_path(str(Path(__file__).with_name("verify_awdp_environment.py")))["suites"]()["maven"]
    repository = root / "maven-repository"
    repository.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="spiritscout-maven-seed-") as temp:
        for filename, content in spec["files"].items():
            destination = Path(temp) / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
        subprocess.run([maven, "--batch-mode", "--settings", "settings.xml", "--global-settings", "settings.xml",
                        f"-Dmaven.repo.local={repository}", "compile"],
                       cwd=temp, check=True, timeout=300,
                       env={**os.environ, "MAVEN_OPTS": "-Xmx256m"})
    manifest = {
        "typescript": TYPESCRIPT_VERSION, "gradle": GRADLE_VERSION,
        "gradle_sha256": GRADLE_SHA256, "root": str(root),
        "maven_cache": str(repository), "maven_plugins": {"compiler": "3.14.1", "resources": "3.3.1"},
        "qualification": "仅准备工具；实际可用性以断网沙箱编译验收为准",
    }
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv", type=Path, default=Path(__file__).resolve().parents[1] / ".venv")
    parser.add_argument("--install", action="store_true", help="下载并安装；默认只显示计划")
    args = parser.parse_args()
    if not args.install:
        print(json.dumps({"venv": str(args.venv.resolve()), "typescript": TYPESCRIPT_VERSION,
                          "gradle": GRADLE_VERSION, "gradle_sha256": GRADLE_SHA256}, indent=2))
        return 0
    print(json.dumps(prepare(args.venv.resolve()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
