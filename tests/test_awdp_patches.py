"""AWDP 提交件的路径隔离、原件比对与可下载交付回归。"""
from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from spiritscout.awdp.patches import validate_patch
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import RunMode, Task
from spiritscout.ctf.agents import CTFWebAgent
from spiritscout.ctf.flags import FlagManager
from spiritscout.tools.sandbox import sandbox_available, sandbox_exec
from spiritscout.tools.sandbox_paths import sandbox_tmp_directory
from spiritscout.web.deliverables import open_artifact, read_deliverables


class PatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rundir = self.root / "sandbox" / "one"
        self.rundir.mkdir(parents=True)
        (self.root / "attachments").mkdir()
        self.original = self.root / "attachments" / "ruby.zip"
        with zipfile.ZipFile(self.original, "w") as z:
            z.writestr("attachment/app.rb", b"original code\n")
            z.writestr("attachment/config.ru", b"config\n")
        self.archive = self.rundir / "fix.tar.gz"
        self.make_tar()

    def make_tar(self, members: list[tuple[str, bytes]] | None = None,
                 *, mode: str = "w:gz") -> None:
        members = members or [("attachment/app.rb", b"patched code\n")]
        with tarfile.open(self.archive, mode) as t:
            for name, data in members:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o755
                t.addfile(info, io.BytesIO(data))

    def validate(self, archive: str = "fix.tar.gz", files=None):
        return validate_patch(self.root, "one", archive,
                              ["attachment/app.rb"] if files is None else files,
                              [str(self.original)])

    def test_relative_archive_is_published_without_rewriting_tree(self) -> None:
        patch = self.validate()
        self.assertEqual(patch.files, ("attachment/app.rb",))
        self.assertEqual(self.archive.read_bytes(), patch.path.read_bytes())
        relative = patch.path.relative_to(self.root).as_posix()
        with open_artifact(self.root, relative) as f:
            self.assertEqual(f.read(), self.archive.read_bytes())
        items = read_deliverables(self.root, kind="file")["items"]
        self.assertIn(relative, {i["path"] for i in items})

    def test_absolute_archive_and_sandbox_tmp_path_are_supported(self) -> None:
        self.validate(str(self.archive))
        target = self.root / sandbox_tmp_directory("one") / "fix.tar.gz"
        target.parent.mkdir(parents=True)
        target.write_bytes(self.archive.read_bytes())
        self.validate("/tmp/fix.tar.gz")

    def test_schema_string_is_not_silently_split_into_characters(self) -> None:
        with self.assertRaisesRegex(ValueError, "字符串数组"):
            self.validate(files='["attachment/app.rb"]')

    def test_empty_or_duplicate_files_list_is_rejected(self) -> None:
        for files in ([], ["attachment/app.rb", "attachment/app.rb"]):
            with self.subTest(files=files), self.assertRaises(ValueError):
                self.validate(files=files)

    def test_filename_does_not_substitute_for_valid_gzip_tar(self) -> None:
        self.archive.write_text("not a tarball")
        with self.assertRaisesRegex(ValueError, "gzip"):
            self.validate()
        self.make_tar(mode="w")
        with self.assertRaisesRegex(ValueError, "gzip"):
            self.validate()

    def test_original_structure_and_changed_bytes_are_required(self) -> None:
        self.make_tar([("app.rb", b"patched")])
        with self.assertRaisesRegex(ValueError, "原始附件不一致"):
            self.validate(files=["app.rb"])
        self.make_tar([("attachment/app.rb", b"original code\n")])
        with self.assertRaisesRegex(ValueError, "未修改"):
            self.validate()

    def test_declared_files_must_match_actual_contents(self) -> None:
        self.make_tar([("attachment/config.ru", b"modified")])
        with self.assertRaisesRegex(ValueError, "未声明"):
            self.validate()
        self.make_tar([("attachment/app.rb", b"patched")])
        with self.assertRaisesRegex(ValueError, "清单不一致"):
            self.validate(files=["attachment/app.rb", "attachment/config.ru"])

    def test_duplicate_tar_paths_and_links_are_rejected(self) -> None:
        self.make_tar([("attachment/app.rb", b"x"), ("attachment/app.rb", b"y")])
        with self.assertRaisesRegex(ValueError, "重复归档"):
            self.validate()
        with tarfile.open(self.archive, "w:gz") as t:
            info = tarfile.TarInfo("attachment/app.rb")
            info.type = tarfile.SYMTYPE
            info.linkname = "../../outside"
            t.addfile(info)
        with self.assertRaisesRegex(ValueError, "链接"):
            self.validate()

    def test_cross_topic_and_external_paths_cannot_be_submitted(self) -> None:
        other = self.root / "sandbox" / "two" / "fix.tar.gz"
        other.parent.mkdir()
        other.write_bytes(self.archive.read_bytes())
        for raw in (str(other), "/etc/passwd", "../two/fix.tar.gz"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.validate(raw)

    def test_archive_symlinks_and_artifact_directory_symlinks_are_rejected(self) -> None:
        real = self.archive.with_name("real.tar.gz")
        self.archive.rename(real)
        self.archive.symlink_to(real)
        with self.assertRaises(ValueError):
            self.validate()
        self.archive.unlink()
        real.rename(self.archive)
        target = self.root / "outside"
        target.mkdir()
        (self.root / "artifacts").symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            self.validate()
        self.assertEqual(list(target.iterdir()), [])

    def test_publish_uses_validated_snapshot_even_if_source_changes(self) -> None:
        from spiritscout.awdp.patches import _original_digests

        validated_bytes = self.archive.read_bytes()
        def changed_source(*args):
            digests = _original_digests(*args)
            self.archive.write_bytes(b"replaced during validation")
            return digests
        with patch("spiritscout.awdp.patches._original_digests", side_effect=changed_source):
            result = self.validate()
        self.assertEqual(result.path.read_bytes(), validated_bytes)
        self.assertNotEqual(self.archive.read_bytes(), validated_bytes)

    def test_archive_traversal_is_rejected_without_extraction(self) -> None:
        self.make_tar([("../../escaped.rb", b"bad")])
        with self.assertRaisesRegex(ValueError, "目录穿越"):
            self.validate(files=["../../escaped.rb"])
        self.assertFalse((self.root.parent / "escaped.rb").exists())


class SubmitFixTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(sandbox_available(), "需要 Linux/bubblewrap")
    async def test_real_sandbox_tmp_archive_is_validated_and_downloadable(self) -> None:
        fixture = PatchTests("test_relative_archive_is_published_without_rewriting_tree")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        result = await sandbox_exec(
            "mkdir -p attachment; printf 'patched code\\n' > attachment/app.rb; "
            "tar czf /tmp/fix.tar.gz attachment/app.rb",
            workspace_root=str(fixture.root), workspace_scope="one",
            _run_mode=RunMode.AWDP, _lightweight_local=True)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.returncode, 0, result.stderr)
        validated = fixture.validate("/tmp/fix.tar.gz")
        with open_artifact(fixture.root, validated.path.relative_to(fixture.root).as_posix()) as f:
            with tarfile.open(fileobj=f, mode="r:gz") as archive:
                self.assertEqual(archive.getnames(), ["attachment/app.rb"])

    async def test_submit_reads_builtin_workspace_and_persists_completion(self) -> None:
        fixture = PatchTests("test_relative_archive_is_published_without_rewriting_tree")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        agent = object.__new__(CTFWebAgent)
        agent.agent_id = "fix-agent"
        agent.ctx = SimpleNamespace(mode=RunMode.AWDP,
            builtin_ctx={"workspace_root": str(fixture.root)}, audit=NullAuditLogger())
        agent.flag_manager = FlagManager(fix_mode=True, local_only=True,
                                         store_path=fixture.root / "flags.json")
        task = Task(task_id="fix", agent_type="ctf_web", task_type="ctf_web", target="",
            payload={"challenge": {"id": "one", "attachments": [str(fixture.original)]}})
        result = await agent._tool_submit_fix(task, SimpleNamespace(name="solve", route_id="r"),
            {"archive": "fix.tar.gz", "files": ["attachment/app.rb"],
             "description": "Remove unsafe reflection"})
        self.assertTrue(result.ok, result.text)
        self.assertTrue(result.stop)
        self.assertIn("可下载", result.text)
        loaded = FlagManager(fix_mode=True, local_only=True,
                             store_path=fixture.root / "flags.json")
        self.assertEqual(loaded.completed_ids(), {"one"})
        fixture.archive.write_text("bad")
        bad = await agent._tool_submit_fix(task, SimpleNamespace(name="solve", route_id="r"),
            {"archive": "fix.tar.gz", "files": ["attachment/app.rb"], "description": "bad"})
        self.assertFalse(bad.ok)
        self.assertEqual(len(agent.flag_manager.fixes), 1)

    async def test_ctf_mode_cannot_enter_fix_handler(self) -> None:
        agent = object.__new__(CTFWebAgent)
        agent.flag_manager = FlagManager(local_only=True, fix_mode=True)
        agent.ctx = SimpleNamespace(mode=RunMode.CTF)
        result = await agent._tool_submit_fix(None, None,
            {"archive": "fix.tar.gz", "files": ["file"], "description": "desc"})
        self.assertFalse(result.ok)
        self.assertEqual(agent.flag_manager.fixes, {})
