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

from spiritscout.platform_support import app_root, bundle_root, is_frozen
from spiritscout.updates.bootstrap import redirect

redirect()

# litellm 导入期会同步联网拉模型价格表（3 次重试+退避，慢网实测 22s+，
# 且跑在事件循环上会把整个控制台冻住）；官方开关改用内置表。本网络下
# 远端表本来就永远拉不到、每次都回退同一份内置表，设此开关只是省掉
# 注定失败的网络往返（取值必须为 True，"1" 不生效）。须在任何
# litellm 导入前设置。
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from spiritscout import __version__  # noqa: E402 — 更新环境切换必须先于服务依赖导入。
from spiritscout.config import load_model_config, load_system_config  # noqa: E402
from spiritscout.core.exceptions import SpiritScoutError  # noqa: E402


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
    candidates = (Path.cwd() / ".env",
                  app_root() / ".env")
    return next((path for path in candidates if path.is_file()), None)


def _prepare_portable_runtime() -> None:
    """打包资源只读，配置与工作区落在用户数据目录；绝不覆盖已有文件。"""
    if not is_frozen():
        return
    root, bundled = app_root(), bundle_root()
    root.mkdir(parents=True, exist_ok=True)
    os.chdir(root)
    (root / "configs").mkdir(exist_ok=True)
    for template in (bundled / "configs").glob("*.example.yaml"):
        target = root / "configs" / template.name
        if not target.exists():
            shutil.copyfile(template, target)
    target = root / "configs/system.yaml"
    if not target.exists():
        shutil.copyfile(bundled / "configs/system.example.yaml", target)
    models = root / "configs/models.yaml"
    if not models.exists():
        models.write_text("providers: []\nmodels: []\n", encoding="utf-8")
    cache = bundled / "tiktoken-cache"
    if cache.is_dir():
        os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(cache))


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
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list[:1] == ["--run-chrome-mcp"]:
        from spiritscout.tools.chrome_mcp import main as chrome_main

        return chrome_main()
    if args_list[:1] == ["--internal-update-worker"]:
        from spiritscout.updates.worker import run

        return run(Path(args_list[1]))
    # 英文 Windows 的重定向输出可能默认 cp1252，中文帮助/错误信息也须能输出。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args(args_list)
    try:
        _prepare_portable_runtime()
    except OSError as exc:
        print(f"[错误] 无法初始化运行目录：{exc}", file=sys.stderr)
        return 2
    dotenv = _find_dotenv()
    if dotenv:
        _load_dotenv(dotenv)      # 忘记管理员凭据时改 .env 重启即可恢复
    try:
        cfg = load_system_config(args.config)
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
        app.state.updater.configure_launch(args_list, host, port, args.config)
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
