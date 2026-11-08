"""验证构建白名单、架构、冻结程序可写目录；不安装到宿主系统。"""

from __future__ import annotations

import importlib.util
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spiritscout.__main__ import _prepare_portable_runtime
from spiritscout.platform_support import app_root
from spiritscout.updates.bootstrap import state_root
from spiritscout.updates.source import installation

_SPEC = importlib.util.spec_from_file_location(
    "linux_packages",
    Path(__file__).resolve().parents[1] / "scripts/build-linux-packages.py",
)
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)


class PackagingTests(unittest.TestCase):
    def test_snapshot_excludes_secrets_and_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root, stage = Path(temp) / "repo", Path(temp) / "stage"
            root.mkdir()
            stage.mkdir()
            for directory in (
                "src/spiritscout",
                "packaging",
                "configs",
                ".spiritscout",
            ):
                (root / directory).mkdir(parents=True)
            for name in (
                "pyproject.toml",
                "README.md",
                "LICENSE",
                ".env.example",
                "configs/system.example.yaml",
                "src/spiritscout/__init__.py",
            ):
                (root / name).write_text("public")
            for name in (
                ".env",
                "configs/models.yaml",
                "configs/system.yaml",
                ".spiritscout/vault.key",
            ):
                (root / name).write_text("private-sentinel")
            with patch.object(build, "ROOT", root):
                build.snapshot(stage)
            self.assertFalse((stage / ".env").exists())
            self.assertFalse((stage / "configs/models.yaml").exists())
            self.assertFalse((stage / ".spiritscout").exists())
            self.assertTrue((stage / "configs/system.example.yaml").exists())
            self.assertFalse(
                any(
                    b"private-sentinel" in p.read_bytes()
                    for p in stage.rglob("*")
                    if p.is_file()
                )
            )

    def test_repeated_license_copy_does_not_modify_readonly_module_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp) / "source", Path(temp) / "target"
            source.write_text("upstream license")
            source.chmod(0o444)
            target.write_text("old")
            target.chmod(0o444)
            build.copy_license(source, target)
            build.copy_license(source, target)
            self.assertEqual(target.read_text(), "upstream license")
            self.assertEqual(source.stat().st_mode & 0o777, 0o444)

    def test_architecture_comes_from_elf_header(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "binary"
            for machine, expected in ((62, "x86_64"), (183, "aarch64")):
                data = bytearray(64)
                data[:6] = b"\x7fELF\x02\x01"
                struct.pack_into("<H", data, 18, machine)
                path.write_bytes(data)
                self.assertEqual(build.elf_arch(path), expected)
            path.write_text("not an ELF")
            with self.assertRaises(ValueError):
                build.elf_arch(path)

    def test_frozen_runtime_uses_user_directory_and_preserves_config(self):
        cwd = Path.cwd()
        self.addCleanup(os.chdir, cwd)
        with tempfile.TemporaryDirectory() as temp:
            root, bundle = Path(temp) / "user", Path(temp) / "_MEI-bundle"
            (bundle / "configs").mkdir(parents=True)
            (bundle / "configs/system.example.yaml").write_text(
                "workspace:\n  root: .spiritscout\n"
            )
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "_MEIPASS", str(bundle), create=True),
                patch.dict(os.environ, {"SPIRITSCOUT_DATA_HOME": str(root)}),
            ):
                self.assertEqual(app_root(), root)
                _prepare_portable_runtime()
                (root / "configs/models.yaml").write_text("private fixture")
                _prepare_portable_runtime()
                self.assertEqual(
                    (root / "configs/models.yaml").read_text(), "private fixture"
                )
                self.assertEqual(Path.cwd(), root)
                with patch.dict(os.environ, {}, clear=True):
                    os.environ["SPIRITSCOUT_DATA_HOME"] = str(root)
                    self.assertEqual(state_root(), root / ".spiritscout-updates")

    def test_system_package_does_not_offer_binary_overwrite(self):
        with (
            patch("spiritscout.updates.source.containerized", return_value=False),
            patch.dict(os.environ, {"SPIRITSCOUT_PACKAGE_FORMAT": "arch"}),
            patch.object(sys, "frozen", True, create=True),
        ):
            result = installation()
            self.assertEqual(result["kind"], "system_package")
            self.assertFalse(result["supported"])


if __name__ == "__main__":
    unittest.main()
