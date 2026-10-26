"""浏览器 MCP：SRC 连接现有 CDP，自动解题使用独立无头浏览器。"""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

MCP_PACKAGE = "@playwright/mcp@0.0.83"
PACKAGED_CLI = Path("/opt/chrome-mcp/node_modules/@playwright/mcp/cli.js")


def default_host() -> str:
    """容器与 WSL 分别使用各自的 Windows 网关，不能混用两个地址。"""
    if Path("/.dockerenv").exists():
        return "host.docker.internal"
    release = Path("/proc/sys/kernel/osrelease")
    if release.exists() and "microsoft" in release.read_text().lower():
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 4 and fields[1] == "00000000" and int(fields[3], 16) & 2:
                return socket.inet_ntoa(bytes.fromhex(fields[2])[::-1])
        raise ValueError("找不到 WSL 网关，请设置 SPIRITSCOUT_CHROME_CDP_URL")
    return "127.0.0.1"


def cdp_endpoint(value: str = "") -> str:
    """Chrome 的 CDP Host 需要 IP 字面量或 localhost；将 Docker 别名解析为 IP。"""
    value = value or f"http://{default_host()}:9223"
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("SPIRITSCOUT_CHROME_CDP_URL 必须是 HTTP(S) CDP 地址")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("此 Chrome kit 不支持带用户凭据的 CDP 地址")
    port = parsed.port  # 在 DNS 之前校验端口，非法配置不触发网络查询。
    host = parsed.hostname
    if host != "localhost":
        try:
            ipaddress.ip_address(host)
        except ValueError:
            host = socket.gethostbyname(host)
    if ":" in host:
        host = f"[{host}]"
    if port is not None:
        host += f":{port}"
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, ""))


def command(endpoint: str, env: dict[str, str]) -> list[str]:
    """容器使用构建期固定的 CLI；源码部署支持现有 CLI 或 npx 安装。"""
    explicit = env.get("SPIRITSCOUT_CHROME_MCP_CLI", "")
    cli = Path(explicit) if explicit else PACKAGED_CLI
    if explicit and not cli.is_file():
        raise ValueError("SPIRITSCOUT_CHROME_MCP_CLI 指定的文件不存在")
    if cli.is_file():
        node = shutil.which("node")
        if node is None:
            raise ValueError("找不到 Node.js，请安装 Node.js 22+")
        prefix = [node, str(cli.resolve())]
    else:
        npx = shutil.which("npx")
        if npx is None:
            raise ValueError("找不到 npx，请安装 Node.js 22+，或使用已内置 Chrome MCP 的 Docker 镜像")
        prefix = [npx, "--yes", MCP_PACKAGE]
    output = Path(env["SPIRITSCOUT_CHROME_OUTPUT_DIR"]) if env.get("SPIRITSCOUT_CHROME_OUTPUT_DIR") else (
        Path.cwd() / ".tools" / "chrome-mcp" / "output")
    output.mkdir(parents=True, exist_ok=True)
    env.setdefault("npm_config_cache", str(Path.cwd() / ".tools" / "chrome-mcp" / "npm-cache"))
    mode = env.get("SPIRITSCOUT_CHROME_MODE", "cdp")
    if mode == "headless":
        # 使用本机已安装浏览器；配置目录由 MCP 隔离，不读取用户 Chrome 登录态。
        executable = env.get("SPIRITSCOUT_CHROME_EXECUTABLE", "") or next(
            (found for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
             if (found := shutil.which(name))), "")
        connection = ["--headless", "--isolated"]
        if executable:
            connection += ["--executable-path", executable]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            connection.append("--no-sandbox")
    elif mode == "cdp":
        connection = ["--cdp-endpoint", endpoint]
    else:
        raise ValueError("SPIRITSCOUT_CHROME_MODE 必须是 cdp 或 headless")
    return [*prefix, *connection, "--caps", "storage",
            "--snapshot-boxes", "--output-dir", str(output)]


def main() -> int:
    try:
        env = dict(os.environ)
        endpoint = ("" if env.get("SPIRITSCOUT_CHROME_MODE") == "headless" else
                    cdp_endpoint(env.get("SPIRITSCOUT_CHROME_CDP_URL", "")))
        argv = command(endpoint, env)
        # stdout 只承载 MCP 协议；替换进程后由原有 MCP 生命周期负责回收连接。
        os.execvpe(argv[0], argv, env)
    except (OSError, ValueError) as exc:
        print(f"[chrome MCP] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
