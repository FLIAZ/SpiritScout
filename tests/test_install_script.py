"""在临时仓库使用真实 venv/pip 安装本地夹具，验证安装入口；不联网。"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import venv
from pathlib import Path


@unittest.skipUnless(
    shutil.which("bash") and sys.platform in {"linux", "darwin"},
    "需要 Linux/macOS 的 Bash 和 Python venv",
)
class InstallScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="spiritscout install ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copy2(Path(__file__).resolve().parents[1] / "install.sh",
                     self.root / "install.sh")
        (self.root / "configs").mkdir()
        (self.root / "configs/system.example.yaml").write_text(
            "workspace:\n  root: .spiritscout\n", encoding="utf-8",
        )
        (self.root / "pyproject.toml").write_text(textwrap.dedent('''\
            [build-system]
            requires = []
            build-backend = "fixture_backend"
            backend-path = ["."]
        '''), encoding="utf-8")
        # 无构建依赖的可编辑安装夹具，真实经过 pip 的 PEP 517/660 路径。
        (self.root / "fixture_backend.py").write_text(textwrap.dedent('''\
            import os
            from pathlib import Path
            from zipfile import ZipFile

            def build_editable(wheel_directory, config_settings=None,
                               metadata_directory=None):
                if os.environ.get("INSTALL_FIXTURE_FAIL_BUILD"):
                    raise RuntimeError("fixture build failure")
                name = "installation_fixture-0.0.0-py3-none-any.whl"
                info = "installation_fixture-0.0.0.dist-info"
                with ZipFile(Path(wheel_directory) / name, "w") as wheel:
                    wheel.writestr("installation_fixture.pth", str(Path.cwd()))
                    wheel.writestr(info + "/METADATA",
                        "Metadata-Version: 2.1\\nName: installation-fixture\\n"
                        "Version: 0.0.0\\n")
                    wheel.writestr(info + "/WHEEL",
                        "Wheel-Version: 1.0\\nRoot-Is-Purelib: true\\n"
                        "Tag: py3-none-any\\n")
                    wheel.writestr(info + "/RECORD", "")
                return name
        '''), encoding="utf-8")
        (self.root / "installation_probe.py").write_text(
            "VALUE = 'installed'\n", encoding="utf-8",
        )
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("PIP_")}
        self.env.update(
            SPIRITSCOUT_PYTHON=sys._base_executable,
            PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1",
            PIP_CONFIG_FILE=os.devnull, PIP_NO_CACHE_DIR="1",
        )

    def install(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.root / "install.sh"), *args],
            cwd=self.root.parent, env=self.env, capture_output=True, text=True,
            timeout=60, check=False,
        )

    def assert_success(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("核心依赖和配置已准备好", result.stdout)

    def test_fresh_install_and_rerun_preserve_existing_files(self) -> None:
        self.assert_success(self.install())
        template = self.root / "configs/system.example.yaml"
        self.assertEqual((self.root / "configs/system.yaml").read_bytes(),
                         template.read_bytes())
        self.assertEqual((self.root / "configs/models.yaml").read_text(),
                         "providers: []\nmodels: []\n")
        probe = subprocess.run(
            [str(self.root / ".venv/bin/python"), "-c",
             "import installation_probe; print(installation_probe.VALUE)"],
            cwd=self.root.parent, capture_output=True, text=True, check=False,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout.strip(), "installed")
        protected = {
            "configs/system.yaml": b"existing system\n",
            "configs/models.yaml": b"existing models\n",
            "configs/agent_models.yaml": b"existing assignments\n",
            ".env": b"fixture credential\n",
            ".spiritscout/fixture.db": b"existing data\x00",
            ".venv/fixture-marker": b"existing environment\n",
        }
        for name, content in protected.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        # 已有可用环境时，忽略只用于新建环境的解释器选项。
        self.env["SPIRITSCOUT_PYTHON"] = str(self.root / "missing-python")
        self.assert_success(self.install())
        for name, content in protected.items():
            self.assertEqual((self.root / name).read_bytes(), content, name)

    def test_dependency_failure_stops_before_config_initialization(self) -> None:
        self.env["INSTALL_FIXTURE_FAIL_BUILD"] = "1"
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("依赖安装失败", result.stderr)
        self.assertNotIn("核心依赖和配置已准备好", result.stdout)
        self.assertFalse((self.root / "configs/system.yaml").exists())
        self.assertFalse((self.root / "configs/models.yaml").exists())

    def test_existing_environment_without_pip_is_repaired_and_checked(self) -> None:
        venv.EnvBuilder(with_pip=False).create(self.root / ".venv")
        # 已有第三方包的依赖冲突应被 pip check 报出，不能假报安装成功。
        site = next((self.root / ".venv/lib").glob("python*/site-packages"))
        metadata = site / "broken_fixture-0.0.0.dist-info/METADATA"
        metadata.parent.mkdir()
        metadata.write_text(
            "Metadata-Version: 2.1\nName: broken-fixture\nVersion: 0.0.0\n"
            "Requires-Dist: missing-install-fixture-dependency\n",
            encoding="utf-8",
        )
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("依赖检查失败", result.stderr)
        self.assertNotIn("核心依赖和配置已准备好", result.stdout)
        self.assertFalse((self.root / "configs/models.yaml").exists())
        self.assertTrue(metadata.is_file())

    def test_foreign_environment_is_preserved_and_help_does_not_install(self) -> None:
        self.assertEqual(self.install("--help").returncode, 0)
        self.assertNotEqual(self.install("--unknown").returncode, 0)
        self.assertFalse((self.root / ".venv").exists())
        foreign_python = self.root / ".venv/Scripts/python.exe"
        foreign_python.parent.mkdir(parents=True)
        foreign_python.write_bytes(b"foreign environment")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("请先将它移到备份目录", result.stderr)
        self.assertEqual(foreign_python.read_bytes(), b"foreign environment")
        self.assertFalse((self.root / ".venv/bin").exists())


if __name__ == "__main__":
    unittest.main()
