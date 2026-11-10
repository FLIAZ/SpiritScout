"""Linux 命令出口隔离：网络命名空间中的 HTTP 代理经 Unix socket 接回会话网关。"""
from __future__ import annotations

import shutil
import sys

# 在子命名空间内执行，不能依赖可被沙箱遮蔽的仓库路径或第三方包。
RELAY = r'''
import os, socket, subprocess, sys, threading
from urllib.parse import urlsplit, urlunsplit

bridge = sys.argv[1]
listener = socket.socket()
listener.bind(('127.0.0.1', 0))
listener.listen(64)
port = listener.getsockname()[1]

def pump(source, destination):
    try:
        while True:
            block = source.recv(65536)
            if not block:
                break
            destination.sendall(block)
    except OSError:
        pass
    finally:
        try:
            destination.shutdown(socket.SHUT_WR)
        except OSError:
            pass

def forward(client):
    upstream = socket.socket(socket.AF_UNIX)
    try:
        upstream.connect(bridge)
        worker = threading.Thread(target=pump, args=(client, upstream), daemon=True)
        worker.start()
        pump(upstream, client)
        worker.join()
    except OSError:
        pass
    finally:
        client.close()
        upstream.close()

def serve():
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return
        threading.Thread(target=forward, args=(client,), daemon=True).start()

threading.Thread(target=serve, daemon=True).start()
environment = dict(os.environ)
for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
    old = urlsplit(environment.get(key, ''))
    credentials = old.netloc.rsplit('@', 1)[0] + '@' if '@' in old.netloc else ''
    environment[key] = urlunsplit(('http', credentials + '127.0.0.1:' + str(port), '', '', ''))
environment['NO_PROXY'] = environment['no_proxy'] = ''
try:
    sys.exit(subprocess.call(sys.argv[2:], env=environment))
finally:
    listener.close()
'''


def relay_command(command, socket_path):
    return [sys.executable, '-c', RELAY, socket_path, *command]


def isolate_command(command, environment, *, isolate_pids=True):
    socket_path = environment.get('SPIRITSCOUT_EGRESS_SOCKET')
    if not socket_path:
        return command
    binary = shutil.which('bwrap')
    if not binary:
        raise ValueError('固定代理命令出口需要 bubblewrap；未执行命令')
    return [binary, '--bind', '/', '/', '--dev', '/dev', '--unshare-net',
            *(['--unshare-pid', '--proc', '/proc'] if isolate_pids else []), '--die-with-parent', '--',
            *relay_command(command, socket_path)]
