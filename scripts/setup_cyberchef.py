"""安装锁定的官方 CyberChef Node API，不修改系统 Node 或用户配置。"""
from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


def main() -> None:
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=project / '.tools/cyberchef')
    parser.add_argument('--node', default=shutil.which('node'), help='Node.js 24–26 可执行文件')
    args = parser.parse_args()
    if not args.node or not shutil.which('npm'):
        parser.error('请先安装 Node.js 24–26 和 npm')
    node = Path(args.node).resolve()
    version = subprocess.check_output([str(node), '--version'], text=True).strip()
    if not 24 <= int(version.lstrip('v').split('.')[0]) <= 26:
        parser.error(f'当前为 {version}，CyberChef 需要 Node.js 24–26；可用 --node 指定独立运行时')
    root = args.directory.resolve()
    (root / 'bin').mkdir(parents=True, exist_ok=True)
    installed_node = root / 'bin/node'
    if node != installed_node:
        shutil.copy2(node, installed_node)
    for name in ('package.json', 'package-lock.json'):
        shutil.copy2(project / 'deploy/cyberchef' / name, root / name)
    npm = str(Path(shutil.which('npm')).resolve())
    subprocess.run([str(installed_node), npm, 'ci', '--prefix', str(root),
                    '--ignore-scripts', '--omit=dev', '--no-audit', '--no-fund'], check=True)
    # 官方 postinstall 中 Node API 所需步骤，直接执行，不拉取 grunt 或 UI 构建链。
    subprocess.run([str(installed_node), str(root / 'node_modules/cyberchef/src/core/config/scripts/fixCryptoApiImports.mjs')],
                   cwd=root, check=True)
    subprocess.run([str(installed_node), '--input-type=module', '-e',
                    'import chef from "cyberchef"; '
                    'const d=await chef.bake("YWJj", [{op:"From Base64",args:["A-Za-z0-9+/=",false,true]}]); '
                    'if(Buffer.from(d.get("byteArray")).toString("hex")!=="616263")process.exit(1);'],
                   cwd=root, check=True)
    print(f'CyberChef 安装并通过真实解码验证：{root}')


if __name__ == '__main__':
    main()
