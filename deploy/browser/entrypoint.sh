#!/bin/bash
set -euo pipefail

# 与控制台使用同一密码；密码只写入容器内的临时认证文件。
python3 - <<'PY'
import base64
import hashlib
import os
from pathlib import Path

password = (os.environ.get("SPIRITSCOUT_ADMIN_PASSWORD", "")
            or os.environ.get("SPIRITSCOUT_SERVER_PASSWORD", ""))
username = os.environ.get("SPIRITSCOUT_ADMIN_USERNAME", "").strip() or "spiritscout"
if not password:
    raise SystemExit("SPIRITSCOUT_ADMIN_PASSWORD or SPIRITSCOUT_SERVER_PASSWORD is required")
if len(username) > 128 or ":" in username or any(ord(ch) < 32 for ch in username):
    raise SystemExit("Invalid browser administrator username")
digest = base64.b64encode(hashlib.sha1(password.encode()).digest()).decode()
Path("/run/browser.htpasswd").write_text(username + ":{SHA}" + digest + "\n")
os.chmod("/run/browser.htpasswd", 0o640)
PY
chown root:www-data /run/browser.htpasswd
unset SPIRITSCOUT_SERVER_PASSWORD SPIRITSCOUT_ADMIN_PASSWORD SPIRITSCOUT_ADMIN_USERNAME
mkdir -p /profile
chown browser:browser /profile
# 容器异常退出后的锁不应阻止同一持久化配置档案再次启动。
rm -f /profile/SingletonLock /profile/SingletonSocket /profile/SingletonCookie

cleanup() {
    trap - EXIT TERM INT
    # 先让 Chromium 退出并刷写 Cookie，再停止显示器和其他组件。
    # 同时终止全部进程会使刚更新的登录态来不及写入配置档案。
    if [ -n "${browser_pid:-}" ]; then
        python3 /usr/local/lib/close_browser.py || true
        for attempt in {1..100}; do
            kill -0 "$browser_pid" 2>/dev/null || break
            sleep 0.1
        done
        pkill -TERM -P "$browser_pid" 2>/dev/null || true
    fi
    kill $(jobs -pr) 2>/dev/null || true
    wait || true
}
trap cleanup EXIT
trap 'exit 0' TERM INT

Xvfb :99 -screen 0 1440x900x24 -nolisten tcp &
for attempt in {1..100}; do
    [ -S /tmp/.X11-unix/X99 ] && break
    sleep 0.1
done
[ -S /tmp/.X11-unix/X99 ]
runuser -u browser -- openbox &
runuser -u browser -- chromium \
    --user-data-dir=/profile --password-store=basic \
    --remote-debugging-port=9222 --no-first-run --no-default-browser-check \
    --disable-dev-shm-usage --window-size=1440,900 about:blank &
browser_pid=$!
x11vnc -display :99 -localhost -nopw -forever -shared -rfbport 5900 &
websockify --web /usr/share/novnc 127.0.0.1:6081 127.0.0.1:5900 &
# Chromium CDP 只监听回环；仅在 Compose 内网提供转发，不映射宿主端口。
socat TCP-LISTEN:9223,fork,reuseaddr TCP:127.0.0.1:9222 &
nginx -g 'daemon off;' &

# 任一组件退出就重启整个服务，避免留下不能操作的浏览器。
set +e
wait -n
result=$?
[ "$result" -ne 0 ] || result=1
exit "$result"
