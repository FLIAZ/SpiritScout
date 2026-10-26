"""部署入口的凭据保护和历史数据迁移。"""

import io
import runpy
import tarfile
import tempfile
import unittest
from pathlib import Path

import deploy

initialize = runpy.run_path(str(Path(deploy.__file__).parent / "deploy/container_entrypoint.py"))["initialize"]


class DeploymentTests(unittest.TestCase):
    def test_credentials_are_generated_once_and_never_printed_or_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env.example").write_text("SPIRITSCOUT_ADMIN_PASSWORD=please-change-me\n", encoding="utf-8")
            self.assertTrue(deploy.init_environment(root))
            content = (root / ".env").read_text()
            self.assertNotIn("please-change-me", content)
            self.assertFalse(deploy.init_environment(root))
            self.assertEqual((root / ".env").read_text(), content)

    def test_blank_model_configuration_is_created_without_overwriting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize(root)
            (root / "models.yaml").write_text("models: [user-configuration]", encoding="utf-8")
            initialize(root)
            self.assertEqual((root / "models.yaml").read_text(), "models: [user-configuration]")

    def archive(self, name, content=b"fixture"):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        stream.seek(0)
        return stream

    def test_migration_preserves_files_and_refuses_existing_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            deploy.restore_stream(self.archive("data/session/metadata.json"), root / "data", root / "configs")
            self.assertEqual((root / "data/session/metadata.json").read_bytes(), b"fixture")
            with self.assertRaises(RuntimeError):
                deploy.restore_stream(self.archive("data/overwrite"), root / "data", root / "configs")

    def test_migration_rejects_parent_paths_and_links(self):
        for name in ("data/../../escape", "data/C:/escape", "/tmp/escape"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                with self.assertRaises((RuntimeError, OSError)):
                    deploy.restore_stream(self.archive(name), root / "data", root / "configs")
