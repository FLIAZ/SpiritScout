"""沙箱遮蔽参数：控制面根目录整层不可见、会话产物随后重新开放。

只遮蔽会话自身目录时，同级会话、vault 密钥库（密钥与密文同处可读＝
全量可解密）与审计在沙箱内照样可读；整层遮蔽基础根后再回挂约定目录。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spiritscout.tools.sandbox import _build_argv, _cuda_devices


class ShadowRootArgvTests(unittest.TestCase):
    def test_cuda_nodes_are_mapped_after_private_dev_without_opening_host_dev(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch('spiritscout.tools.sandbox._cuda_devices',
                return_value=[Path('/dev/nvidia0'), Path('/dev/nvidiactl')]):
            argv = self._argv(Path(tmp), guard=None)
            self.assertLess(argv.index('--dev'), argv.index('--dev-bind'))
            bindings = [argv[i + 1:i + 3] for i, arg in enumerate(argv) if arg == '--dev-bind']
            self.assertEqual(bindings, [['/dev/nvidia0', '/dev/nvidia0'], ['/dev/nvidiactl', '/dev/nvidiactl']])

    def test_cuda_discovery_rejects_regular_files_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'nvidia0').write_text('not a device')
            (root / 'nvidiactl').symlink_to('/dev/null')
            self.assertEqual(_cuda_devices(root), [])

    def test_http_response_snapshots_are_read_only_in_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            responses = workspace / "http-responses"
            responses.mkdir()
            argv = self._argv(workspace, guard=None)
            index = argv.index(str(responses))
            self.assertEqual(argv[index - 1], "--ro-bind")

    def _argv(self, workspace: Path, *, guard: Path | None) -> list[str]:
        rundir = workspace / "sandbox"
        rundir.mkdir(parents=True, exist_ok=True)
        return _build_argv(workspace, rundir, ["bash", "-c", "true"],
                           guard_root=guard)

    def test_control_root_shadowed_before_session_workspace_reopens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            guard = Path(tmp).resolve()
            workspace = guard / "sessions" / "sess-a"
            workspace.mkdir(parents=True)
            argv = self._argv(workspace, guard=guard)
            # 基础根以 tmpfs 整层遮蔽，且先于会话产物目录的开放挂载
            guard_at = argv.index(str(guard))
            self.assertEqual(argv[guard_at - 1], "--tmpfs")
            self.assertLess(guard_at, argv.index(str(workspace)))

    def test_unrelated_guard_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            guard = (Path(tmp) / "elsewhere").resolve()
            guard.mkdir()
            workspace = (Path(tmp) / "ws" / "sess-a").resolve()
            workspace.mkdir(parents=True)
            argv = self._argv(workspace, guard=guard)
            self.assertNotIn(str(guard), argv)

    def test_root_or_self_guard_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = (Path(tmp) / "ws" / "sess-a").resolve()
            workspace.mkdir(parents=True)
            baseline = self._argv(workspace, guard=None)
            self.assertEqual(self._argv(workspace, guard=workspace), baseline)
            self.assertEqual(self._argv(workspace, guard=Path("/")), baseline)


if __name__ == "__main__":
    unittest.main()
