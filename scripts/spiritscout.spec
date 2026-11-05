# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 单文件打包：内置前端、知识数据、报告模板与首启配置模板。

源码运行时不使用本文件；构建入口见 scripts/build-exe.ps1（Windows）与
scripts/build-elf.sh（Linux）。运行期只读资源随包解压，配置与工作区由
spiritscout.__main__ 在可执行文件旁生成。
"""

import os
import sys
import tempfile
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

# 收集 LiteLLM 子模块时会导入包；强制使用内置价格表，避免构建期访问 GitHub。
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

ROOT = Path(SPECPATH).resolve().parent
PKG = ROOT / "src" / "spiritscout"

# 运行期按 __file__ 相对路径读取的资产，缺一不可。
datas = [
    (str(PKG / "web" / "static"), "spiritscout/web/static"),
    (str(PKG / "knowledge" / "data"), "spiritscout/knowledge/data"),
    (str(PKG / "agents" / "report" / "templates"), "spiritscout/agents/report/templates"),
    (str(PKG / "tools" / "cyberchef_worker.mjs"), "spiritscout/tools"),
]
# 首次启动复制到可执行文件旁的配置模板（不直接使用仓库里的本地配置）。
for template in sorted((ROOT / "configs").glob("*.example.yaml")):
    datas.append((str(template), "configs"))
datas.append((str(ROOT / ".env.example"), "."))

# 常用 BPE 编码随包携带，避免首次模型调用时联网下载 tiktoken 词表。
cache_env = os.environ.get("TIKTOKEN_CACHE_DIR")
cache_dir = Path(cache_env) if cache_env else Path(tempfile.gettempdir()) / "data-gym-cache"
try:
    import tiktoken

    for encoding in ("cl100k_base", "o200k_base", "p50k_base", "r50k_base"):
        tiktoken.get_encoding(encoding)
except Exception as exc:  # noqa: BLE001 - 构建继续，运行时仍可尝试下载
    print(f"[警告] 未能缓存 tiktoken 编码：{exc}")
if cache_dir.is_dir():
    datas.append((str(cache_dir), "tiktoken-cache"))

# uvicorn 的协议/生命周期实现、项目内按需导入的模块和 LiteLLM 供应商适配
# 都依赖动态导入，静态分析无法全部发现。
hiddenimports = collect_submodules("spiritscout") + collect_submodules("uvicorn")
# MCP 只使用客户端；mcp.cli 依赖未安装的 typer 可选依赖，按模块显式收集。
hiddenimports += [
    "mcp", "mcp.types", "mcp.client", "mcp.client.session",
    "mcp.client.stdio", "mcp.client.sse", "mcp.client.streamable_http",
    "tiktoken_ext.openai_public",
]
binaries = []
for package in ("litellm", "tzdata"):
    package_datas, package_binaries, package_imports = collect_all(
        package, include_py_files=False, on_error="ignore")
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_imports
for package in ("litellm", "mcp", "fastapi", "uvicorn", "pydantic", "tiktoken", "spiritscout"):
    datas += copy_metadata(package)

a = Analysis(
    [str(ROOT / "scripts" / "pyinstaller_entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SpiritScout" if sys.platform == "win32" else "spiritscout",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
