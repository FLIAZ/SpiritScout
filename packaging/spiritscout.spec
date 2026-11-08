# -*- mode: python ; coding: utf-8 -*-
"""仅打包白名单资源和示例配置；不读取用户工作区、模型清单或 .env。"""
import os
import importlib.metadata
import sysconfig
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
ROOT = Path(SPECPATH).resolve().parent
PKG = ROOT / "src/spiritscout"
datas = [(str(PKG / path), "spiritscout/" + path) for path in (
    "web/static", "knowledge/data", "awdp/data", "agents/report/templates")]
for worker in (PKG / "tools").glob("*.mjs"):
    datas.append((str(worker), "spiritscout/tools"))
for template in (ROOT / "configs").glob("*.example.yaml"):
    datas.append((str(template), "configs"))
datas.append((str(ROOT / ".env.example"), "."))
binaries = []
hiddenimports = collect_submodules("spiritscout") + collect_submodules("uvicorn")
hiddenimports += ["mcp.client.stdio", "mcp.client.sse", "mcp.client.streamable_http", "tiktoken_ext.openai_public"]
for package in ("litellm", "tzdata", "tiktoken_ext"):
    package_data, package_binaries, imports = collect_all(package, include_py_files=False, on_error="warn once")
    datas += package_data
    binaries += package_binaries
    hiddenimports += imports
for distribution in importlib.metadata.distributions():
    datas += copy_metadata(distribution.metadata["Name"])
# 缓存仅来自隔离构建目录，缺失时中止而非生成首次调用需联网的产物。
import tiktoken
for encoding in ("cl100k_base", "o200k_base", "p50k_base", "r50k_base"):
    tiktoken.get_encoding(encoding)
datas.append((os.environ["TIKTOKEN_CACHE_DIR"], "tiktoken-cache"))
# 随包保留隔离环境依赖的许可证，不收集任意用户文件。
for distribution in importlib.metadata.distributions():
    name = distribution.metadata.get("Name", "unknown")
    for relative in distribution.files or []:
        lower = relative.name.lower()
        if lower.startswith(("license", "copying", "notice")) and not str(relative).startswith(".."):
            path = distribution.locate_file(relative)
            if path.is_file():
                datas.append((str(path), "third-party-licenses/" + name + "/" + str(relative.parent)))
python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
if python_license.is_file():
    datas.append((str(python_license), "third-party-licenses/Python"))
a = Analysis([str(ROOT / "packaging/pyinstaller-entry.py")], pathex=[str(ROOT / "src")],
             binaries=binaries, datas=datas, hiddenimports=hiddenimports, hookspath=[],
             excludes=["pytest", "tkinter"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="spiritscout", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=True)
