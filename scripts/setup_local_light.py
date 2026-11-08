"""用标准库生成共用的本地轻量配置；Windows 主机不必先安装项目依赖。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit


def configuration(backend: str, model: str, base_url: str, context: int,
                  output: Path, docker: bool = False) -> dict[str, dict]:
    endpoint = urlsplit(base_url)
    if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
        raise ValueError("base-url 必须是 HTTP(S) 推理服务地址")
    if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError("地址不能包含凭据、查询参数或 fragment")
    if context < 8192:
        raise ValueError("轻量工具上下文至少需要 8192 tokens")
    if not model.strip():
        raise ValueError("model 不能为空")
    data = ".spiritscout-light" if docker else output.as_posix()
    config = "light-config" if docker else output.as_posix()
    base_url = base_url.rstrip("/")
    entry = {"model": model.strip(), "provider_ref": "local-light",
             "context_window": context, "max_output_tokens": 4096,
             "capabilities": ["code", "chinese", "security"], "rate_limit_rpm": 1000}
    if backend == "ollama":
        if base_url.endswith("/v1"):
            base_url = base_url[:-3]
        entry.update(ollama_lightweight=True, ollama_think=False,
                     reasoning_effort_levels=["low", "high"], ollama_keep_alive="30m",
                     ollama_resource="ollama-local-gpu",
                     ollama_options={"num_ctx": context, "num_thread": 8,
                                     "temperature": 0.3, "top_p": 0.9, "top_k": 20,
                                     "repeat_penalty": 1.05})
        provider = "ollama"
    elif backend == "llama_cpp":
        if not base_url.endswith("/v1"):
            base_url += "/v1"
        entry.update(local_runtime="llama_cpp", local_lightweight=True)
        provider = "openai_compatible"
    else:
        raise ValueError("未知本地后端")
    system = {
        "mode": "ctf", "workspace": {"root": data},
        "blackboard": {"sqlite_path": f"{data}/blackboard.db"},
        "knowledge": {"sqlite_path": f"{data}/knowledge.db",
                      "chroma_path": f"{data}/chroma", "embedding_provider": "hash"},
        "gateway": {"request_timeout_seconds": 420, "max_retries": 0,
                    "automatic_platform_extraction": False, "lightweight_local": True,
                    "stable_snapshot_prefix": True},
        "executor": {"backend": "local"}, "search": {"enabled": False},
        "secretary": {"enabled": False}, "experience": {"root": data, "auto_distill": False},
        "report": {"output_dir": f"{data}/reports"},
        "web": {"host": "0.0.0.0" if docker else "127.0.0.1",
                "port": 10010 if docker else 8001,
                "max_concurrent_sessions": 1, "auto_name_sessions": False},
        "resources": {"root": f"{data}/resources"}, "mcp_servers": [],
        "models_config_path": f"{config}/models.yaml",
        "agent_models_path": f"{data}/agent_models.yaml",
        "audit_log_path": f"{data}/audit.jsonl",
        "default_routes_per_agent": 1, "max_routes_per_agent": 2,
    }
    return {"system.yaml": system,
            "models.yaml": {"providers": [{"name": "local-light", "provider": provider,
                                           "base_url": base_url, "api_format": "auto"}],
                            "models": [entry]}}


def write_configuration(output: Path, files: dict[str, dict]) -> None:
    existing = [str(output / name) for name in files if (output / name).exists()]
    if existing:
        raise FileExistsError("保留已有配置：" + ", ".join(existing)
                              + "；切换后端请修改同目录的 models.yaml，保留 system.yaml 和会话工作区。")
    output.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        # JSON 是 YAML 的子集；避免让 Windows 初始化依赖 PyYAML。
        with (output / name).open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(content, stream, ensure_ascii=False, indent=2)
            stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["ollama", "llama_cpp"], required=True)
    parser.add_argument("--model", required=True, help="Ollama 的已有模型名或 llama-server 的 --alias")
    parser.add_argument("--base-url", default="", help="容器默认使用 host.docker.internal")
    parser.add_argument("--context", type=int, default=16384,
                        help="llama.cpp 必须与每个 slot 的实际上下文一致")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--docker", action="store_true")
    args = parser.parse_args(argv)
    # 本地后端只改变模型连接；沿用 offline 工作区，避免按引擎拆分会话。
    output = args.output or Path(".spiritscout-light-config" if args.docker else
                                ".spiritscout-ollama")
    host = "host.docker.internal" if args.docker else "127.0.0.1"
    port = 11434 if args.backend == "ollama" else 8081
    try:
        files = configuration(args.backend, args.model, args.base_url or f"http://{host}:{port}",
                              args.context, output, args.docker)
        write_configuration(output, files)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"初始化未完成：{exc}\n")
    print(f"已创建 {output}/system.yaml 和 models.yaml；未修改或下载模型。")
    if args.docker:
        print("下一步：python deploy.py init，然后 docker compose -f docker-compose.light.yml up -d --build --wait")
    elif args.output is None:
        print("启动：./launch.sh start --offline")
    else:
        print(f"启动：python -m spiritscout --config {output.as_posix()}/system.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
