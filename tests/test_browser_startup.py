"""浏览器显示服务启动的残留锁与就绪契约。"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("bash"), "需要 Bash 执行 Linux 容器入口片段")
class BrowserStartupTests(unittest.TestCase):
    def test_stale_socket_does_not_count_as_display_readiness(self):
        source = (Path(__file__).resolve().parents[1] / "deploy/browser/entrypoint.sh").read_text(
            encoding="utf-8"
        )
        block = source[source.index("rm -f /tmp/.X99-lock"):source.index("runuser -u browser -- openbox")]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".X11-unix").mkdir()
            (root / ".X99-lock").write_text("stale", encoding="utf-8")
            (root / ".X11-unix/X99").touch()
            # 用真实 Bash 验证启动控制流；不启动桌面，不接触用户配置档案。
            script = """set -eu
Xvfb() { exec sleep 30; }
xdpyinfo() {
    test ! -e "$root/.X99-lock" && test ! -e "$root/.X11-unix/X99" || return 1
    echo checked >> "$root/checks"
    test -e "$root/ready" && return 0
    touch "$root/ready"
    return 1
}
trap 'kill "${xvfb_pid:-}" 2>/dev/null || true' EXIT
"""
            result = subprocess.run(
                ["bash", "-c", 'root="$1"\n' + script + block.replace("/tmp/", temporary + "/"),
                 "browser-startup-test", temporary],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertGreaterEqual(len((root / "checks").read_text().splitlines()), 3)


if __name__ == "__main__":
    unittest.main()
