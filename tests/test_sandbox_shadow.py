"""沙箱遮蔽参数：控制面根目录整层不可见、会话产物随后重新开放。

只遮蔽会话自身目录时，同级会话、vault 密钥库（密钥与密文同处可读＝
全量可解密）与审计在沙箱内照样可读；整层遮蔽基础根后再回挂约定目录。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from spiritscout.tools.sandbox import _build_argv


class ShadowRootArgvTests(unittest.TestCase):
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
