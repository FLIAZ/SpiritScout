#!/usr/bin/env python3
"""准备本机 Linux Mihomo：优先复用已有内核，官方下载校验后原子替换。"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen


def install(version: str = 'latest') -> Path:
    if platform.system() != 'Linux':
        raise ValueError('此安装脚本仅用于原生 Linux')
    architecture = {'x86_64': 'amd64-compatible', 'aarch64': 'arm64'}.get(platform.machine())
    if not architecture:
        raise ValueError('此 Linux 架构没有配置安装资产')
    if version != 'latest' and (not version.startswith('v') or any(c not in 'v0123456789.' for c in version)):
        raise ValueError('版本格式应为 v1.2.3 或 latest')
    target = Path(__file__).resolve().parents[1] / '.tools' / 'mihomo'
    target.parent.mkdir(parents=True, exist_ok=True)
    if version == 'latest':
        existing = next((Path(path) for path in (shutil.which('mihomo'), shutil.which('verge-mihomo'),
                         '/usr/bin/verge-mihomo') if path and Path(path).is_file() and os.access(path, os.X_OK)), None)
        if existing:
            result = subprocess.run([str(existing), '-v'], check=True, capture_output=True, text=True, timeout=10)
            if 'Mihomo' not in result.stdout:
                raise ValueError('现有可执行文件未返回 Mihomo 版本信息')
            if existing.resolve() != target.resolve():
                with tempfile.TemporaryDirectory(prefix='.mihomo-install-', dir=target.parent) as temporary:
                    candidate = Path(temporary) / 'mihomo'
                    shutil.copyfile(existing, candidate)
                    candidate.chmod(0o755)
                    os.replace(candidate, target)
            print(result.stdout.strip())
            print(f'已从本机内核准备独立副本：{target}')
            return target
    endpoint = 'latest' if version == 'latest' else 'tags/' + version
    request = Request('https://api.github.com/repos/MetaCubeX/mihomo/releases/' + endpoint,
                      headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'SpiritScout-runtime-setup'})
    with urlopen(request, timeout=30) as response:
        release = json.loads(response.read(2 * 1024 * 1024))
    name = f"mihomo-linux-{architecture}-{release['tag_name']}.gz"
    asset = next((item for item in release['assets'] if item['name'] == name), None)
    if not asset or not str(asset.get('digest', '')).startswith('sha256:'):
        raise ValueError('官方发布缺少匹配资产或 SHA-256 校验值')
    url = asset['browser_download_url']
    if not url.startswith('https://github.com/MetaCubeX/mihomo/releases/download/'):
        raise ValueError('发布资产来源无效')
    with tempfile.TemporaryDirectory(prefix='.mihomo-install-', dir=target.parent) as temporary:
        archive, candidate = Path(temporary) / 'release.gz', Path(temporary) / 'mihomo'
        digest = hashlib.sha256()
        size = 0
        with urlopen(Request(url, headers={'User-Agent': 'SpiritScout-runtime-setup'}), timeout=60) as response, archive.open('wb') as output:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 100 * 1024 * 1024:
                    raise ValueError('Mihomo 下载超过大小上限')
                digest.update(chunk)
                output.write(chunk)
        if 'sha256:' + digest.hexdigest() != asset['digest'] or size != asset['size']:
            raise ValueError('Mihomo 资产校验失败，原有运行时保持不变')
        size = 0
        with gzip.open(archive, 'rb') as source, candidate.open('wb') as output:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > 150 * 1024 * 1024:
                    raise ValueError('Mihomo 解压超过大小上限')
                output.write(chunk)
        candidate.chmod(0o755)
        result = subprocess.run([str(candidate), '-v'], check=True, capture_output=True, text=True, timeout=10)
        os.replace(candidate, target)
    print(result.stdout.strip())
    print(f'已校验并安装：{target}')
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', default='latest', help='指定版本时从官方发布下载；默认优先复用本机 Mihomo 或 Clash Verge 内核')
    install(parser.parse_args().version)
