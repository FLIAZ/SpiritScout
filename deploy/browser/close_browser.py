"""容器退出前请求 Chromium 正常关闭，等待登录态写入配置档案。"""

import base64
import json
import os
import socket
import urllib.request
from urllib.parse import urlsplit


def main() -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:9222/json/version", timeout=2) as response:
        endpoint = urlsplit(json.load(response)["webSocketDebuggerUrl"])
    with socket.create_connection(("127.0.0.1", 9222), timeout=3) as connection:
        key = base64.b64encode(os.urandom(16)).decode()
        request = (f"GET {endpoint.path} HTTP/1.1\r\nHost: 127.0.0.1:9222\r\n"
                   "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        connection.sendall(request.encode())
        headers = b""
        while b"\r\n\r\n" not in headers:
            chunk = connection.recv(4096)
            if not chunk:
                raise OSError("CDP WebSocket closed before handshake")
            headers += chunk
        if not headers.startswith(b"HTTP/1.1 101 "):
            raise OSError("CDP WebSocket handshake failed")
        payload = json.dumps({"id": 1, "method": "Browser.close"}).encode()
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        connection.sendall(bytes([0x81, 0x80 | len(payload)]) + mask + masked)
        connection.recv(4096)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as exc:
        print(f"Browser shutdown: {exc}", flush=True)
