"""灵枢（SpiritScout）Web 控制台启动入口。

用法::

    python -m spiritscout [--config configs/system.yaml] [--host 127.0.0.1] [--port 8000]

会话创建、拓扑观测、编队与模型管理、报告与审计导出均在浏览器内完成。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

# litellm 导入期会同步联网拉模型价格表（3 次重试+退避，慢网实测 22s+，
# 且跑在事件循环上会把整个控制台冻住）；官方开关改用内置表。本网络下
# 远端表本来就永远拉不到、每次都回退同一份内置表，设此开关只是省掉
# 注定失败的网络往返（取值必须为 True，"1" 不生效）。须在任何
# litellm 导入前设置。
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from spiritscout import __version__
from spiritscout.config import load_model_config, load_system_config
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.platform_support import app_root, bundle_root, is_frozen

# 单文件包携带常用 BPE 词表；用户显式指定缓存目录时保持优先。
if is_frozen() and (bundle_root() / "tiktoken-cache").is_dir():
    os.environ.setdefault("TIKTOKEN_CACHE_DIR",
                          str(bundle_root() / "tiktoken-cache"))


def _load_dotenv(path: Path) -> None:
    """读取 .env 补全缺失变量（真实环境变量优先，兼容 CRLF 与引号）。"""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def _find_dotenv() -> Path | None:
    candidates = (Path.cwd() / ".env", app_root() / ".env")
    return next((path for path in candidates if path.is_file()), None)


def _copy_if_missing(source: Path, target: Path) -> bool:
    """只在目标缺失时复制模板，不覆盖用户已有配置。"""
    if target.exists() or not source.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return True


def _prepare_portable_runtime() -> None:
    """冻结构建首次启动：在可执行文件旁生成可编辑配置并固定工作目录。

    单文件打包的解包目录是临时的，配置与工作区必须落在可执行文件旁边；
    双击运行时当前目录通常就是程序目录，这里显式切换以保证终端启动也一致。
    """
    if not is_frozen():
        return
    root = app_root()
    bundled = bundle_root()
    os.chdir(root)
    configs = root / "configs"
    configs.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    if _copy_if_missing(bundled / "configs/system.example.yaml",
                        configs / "system.yaml"):
        created.append("configs/system.yaml")
    models = configs / "models.yaml"
    if not models.exists():
        models.write_text("providers: []\nmodels: []\n", encoding="utf-8")
        created.append("configs/models.yaml")
    for source in sorted((bundled / "configs").glob("*.example.yaml")):
        _copy_if_missing(source, configs / source.name)
    _copy_if_missing(bundled / ".env.example", root / ".env.example")
    if created:
        print(f"[提示] 首次运行已生成 {', '.join(created)}；"
              "可在控制台配置模型，端口与工作区在同目录配置文件中调整。")


def _ensure_default_model_config(path: str) -> None:
    """默认模型注册表缺失时创建空表；自定义路径仍按原样报错。"""
    target = Path(path)
    if (target.exists()
            or target.as_posix().removeprefix("./") != "configs/models.yaml"):
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("providers: []\nmodels: []\n", encoding="utf-8")
    print("[提示] 未找到 configs/models.yaml，已创建空模型注册表；"
          "请在控制台「模型」页添加供应商。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spiritscout",
        description="灵枢（SpiritScout）渗透测试智能体系统 · Web 控制台")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    parser.add_argument("--config", default="configs/system.yaml",
                        help="系统配置 YAML 路径")
    parser.add_argument("--host", default=None,
                        help="绑定地址（默认取配置 web.host=127.0.0.1）")
    parser.add_argument("--port", type=int, default=None,
                        help="端口（默认取配置 web.port=8000）")
    return parser


def main(argv: list[str] | None = None) -> int:
    # 英文 Windows 的重定向输出可能默认 cp1252，中文帮助/错误信息也须能输出。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args_list = list(sys.argv[1:] if argv is None else argv)
    # 冻结版没有独立解释器；配置中的 python -m 子进程由此入口转交。
    if args_list[:1] == ["--run-chrome-mcp"]:
        from spiritscout.tools.chrome_mcp import main as chrome_main

        return chrome_main()
    args = build_parser().parse_args(args_list)
    try:
        _prepare_portable_runtime()
    except OSError as exc:
        print(f"[错误] 无法在程序目录初始化运行环境：{exc}", file=sys.stderr)
        return 2
    dotenv = _find_dotenv()
    if dotenv:
        _load_dotenv(dotenv)      # 忘记管理员凭据时改 .env 重启即可恢复
    try:
        cfg = load_system_config(args.config)
        _ensure_default_model_config(cfg.models_config_path)
        models = load_model_config(cfg.models_config_path)
    except SpiritScoutError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    import uvicorn

    from spiritscout.web import create_app

    host = args.host or cfg.web.host
    port = args.port or cfg.web.port
    # 命令行覆盖不只传给 uvicorn；应用内部的认证策略和生成的
    # 同源边界也必须看到最终绑定值。
    cfg.web.host = host
    cfg.web.port = port
    try:
        app = create_app(cfg, models,
                         max_concurrent=cfg.web.max_concurrent_sessions)
    except SpiritScoutError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    # 预热 litellm：把 ~2s 的库导入成本从「首次调用途中」挪到启动时
    # （调用在事件循环上，冷导入曾把控制台冻 22s+）；失败留待调用路径
    # 原生报错，不阻断启动
    try:
        import litellm  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        print(f"[警告] litellm 预热失败（首次调用时重试）：{exc}", file=sys.stderr)
    auth_note = ("管理员登录已启用" if app.state.auth_enabled
                 else "未配置管理员，仅回环地址可访问")
    print(f"灵枢 Web 控制台: http://{host}:{port}  "
          f"({auth_note}，并发上限 {cfg.web.max_concurrent_sessions}，"
          "Ctrl+C 停止)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
