"""压缩技能导入的真实解析与接口回归。"""

from __future__ import annotations

import io
import shutil
import struct
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from test_resource_management import cfg_at

from spiritscout.resources.archives import _native_entries, parse_skill_archive
from spiritscout.web.app import create_app

SKILL = b'---\nname: sample-skill\ndescription: Archive reference\n---\n# Facts\nSee references/info.md\n'


def zipped(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


class SkillArchiveTests(unittest.TestCase):
    def test_rar_stored_archive(self) -> None:
        # 生成无压缩 RAR4：真实走系统解码器，不用 Mock 代替格式兼容验证。
        def header(raw: bytes) -> bytes:
            return struct.pack('<H', zlib.crc32(raw) & 0xffff) + raw

        name = b'skill/SKILL.md'
        content = b'Rar!\x1a\x07\x00' + header(struct.pack('<BHHHI', 0x73, 0, 13, 0, 0))
        content += header(struct.pack('<BHHIIBIIBBHI', 0x74, 0x8000, 32 + len(name),
                                     len(SKILL), len(SKILL), 3, zlib.crc32(SKILL),
                                     0, 20, 0x30, len(name), 0o100644) + name) + SKILL
        content += header(struct.pack('<BHH', 0x7b, 0, 7))
        import ctypes.util
        if not ctypes.util.find_library('archive'):
            self.skipTest('RAR 验证需要系统 libarchive')
        self.assertEqual(parse_skill_archive(content)[0][0]['skill_id'], 'sample-skill')

    def test_7z_archive(self) -> None:
        import ctypes.util
        command = shutil.which('7z') or shutil.which('7zz')
        if not command or not ctypes.util.find_library('archive'):
            self.skipTest('7z 验证需要 7z/7zz 打包器及系统 libarchive')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'SKILL.md').write_bytes(SKILL)
            subprocess.run([command, 'a', str(root / 'sample.7z'), 'SKILL.md'],
                           cwd=root, capture_output=True, check=True)
            self.assertEqual(parse_skill_archive((root / 'sample.7z').read_bytes())
                             [0][0]['skill_id'], 'sample-skill')

    def test_missing_native_reader_explains_conversion(self) -> None:
        with patch('ctypes.util.find_library', return_value=None):
            with self.assertRaisesRegex(ValueError, 'libarchive'):
                list(_native_entries(b'unsupported'))

    def test_zip_preserves_document_and_references(self) -> None:
        items, warnings = parse_skill_archive(zipped({
            'wrapper/sample/SKILL.md': SKILL,
            'wrapper/sample/references/info.md': '参考事实'.encode(),
            'wrapper/sample/image.png': b'\x89PNG\x00',
        }))
        self.assertEqual(items[0]['skill_id'], 'sample-skill')
        self.assertEqual(items[0]['description'], 'Archive reference')
        self.assertIn('参考事实', items[0]['body'])
        self.assertIn('references/info.md', items[0]['body'])
        self.assertEqual(len(warnings), 1)

    def test_tar_compressions(self) -> None:
        for mode in ('w', 'w:gz', 'w:bz2', 'w:xz'):
            with self.subTest(mode=mode):
                output = io.BytesIO()
                with tarfile.open(fileobj=output, mode=mode) as archive:
                    entry = tarfile.TarInfo('SKILL.md')
                    entry.size = len(SKILL)
                    archive.addfile(entry, io.BytesIO(SKILL))
                self.assertEqual(parse_skill_archive(output.getvalue())[0][0]['skill_id'],
                                 'sample-skill')

    def test_rejects_unsafe_empty_invalid_and_duplicate_archives(self) -> None:
        cases = [b'not an archive', zipped({'readme.md': b'none'}),
                 zipped({'../SKILL.md': SKILL}), zipped({'/SKILL.md': SKILL}),
                 zipped({'C:/SKILL.md': SKILL}),
                 zipped({'one/SKILL.md': SKILL, 'two/SKILL.md': SKILL}),
                 zipped({'SKILL.md': b'---\nname: [bad]\n---\nbody'})]
        for content in cases:
            with self.subTest(content=content[:20]), self.assertRaises(ValueError):
                parse_skill_archive(content)

    def test_rejects_links_and_limits(self) -> None:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            entry = tarfile.TarInfo('SKILL.md')
            entry.type = tarfile.SYMTYPE
            entry.linkname = '/etc/passwd'
            archive.addfile(entry)
        with self.assertRaises(ValueError):
            parse_skill_archive(output.getvalue())
        with patch('spiritscout.resources.archives.MAX_EXPANDED', 16):
            with self.assertRaises(ValueError):
                parse_skill_archive(zipped({'SKILL.md': SKILL}))

    def test_multiple_skills_keep_their_own_references(self) -> None:
        items, _ = parse_skill_archive(zipped({
            'one/SKILL.md': SKILL.replace(b'sample-skill', b'one'),
            'one/ref.md': b'only-one',
            'two/SKILL.md': SKILL.replace(b'sample-skill', b'two'),
            'two/ref.md': b'only-two',
        }))
        self.assertIn('only-one', items[0]['body'])
        self.assertNotIn('only-two', items[0]['body'])

    def test_import_api_conflict_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(cfg_at(Path(directory)), [])
            with TestClient(app, base_url='http://127.0.0.1') as client:
                response = client.post('/api/resources/skills/import',
                                       content=zipped({'SKILL.md': SKILL}))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['items'][0]['skill_id'], 'sample-skill')
                response = client.post('/api/resources/skills/import', content=zipped({
                    'new/SKILL.md': SKILL.replace(b'sample-skill', b'new'),
                    'old/SKILL.md': SKILL,
                }))
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(client.get('/api/resources/skills/new').status_code, 404)
                self.assertIn('# Facts', client.get(
                    '/api/resources/skills/sample-skill').json()['body'])
                with patch('spiritscout.web.resources.MAX_UPLOAD', 8):
                    response = client.post('/api/resources/skills/import', content=b'012345678')
                self.assertEqual(response.status_code, 413)
