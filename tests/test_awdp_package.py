"""最终 tar.gz 的安全解包与赛事入口规范，失败时不写部分文件。"""
from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from spiritscout.awdp.package import extract_package, pack, read_package


class PackageTests(unittest.TestCase):
    def archive(self, members):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            for name, kind, data, mode in members:
                item = tarfile.TarInfo(name)
                item.type, item.mode = kind, mode
                item.size = len(data) if kind == tarfile.REGTYPE else 0
                item.linkname = '../../outside'
                archive.addfile(item, io.BytesIO(data))
        output.seek(0)
        return output

    def test_invalid_member_never_creates_partial_destination(self):
        entry = ('patch.sh', tarfile.REGTYPE, b'#!/bin/sh\nexit 0\n', 0o755)
        cases = [
            [('../outside', tarfile.REGTYPE, b'bad', 0o644)],
            [('/absolute', tarfile.REGTYPE, b'bad', 0o644)],
            [('bad\\name', tarfile.REGTYPE, b'bad', 0o644)],
            [('bad\nname', tarfile.REGTYPE, b'bad', 0o644)],
            [('link', tarfile.SYMTYPE, b'', 0o777)],
            [('link', tarfile.LNKTYPE, b'', 0o644)],
            [('pipe', tarfile.FIFOTYPE, b'', 0o644)],
            [('./patch.sh', tarfile.REGTYPE, b'bad', 0o755)],
            [('setuid', tarfile.REGTYPE, b'bad', 0o4755)],
            [('files', tarfile.REGTYPE, b'bad', 0o644), ('files/app.py', tarfile.REGTYPE, b'bad', 0o644)],
            [('unrelated', tarfile.DIRTYPE, b'', 0o755)],
            [('update.sh', tarfile.REGTYPE, b'#!/bin/sh\n', 0o755)],
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for index, members in enumerate(cases):
                with self.subTest(members=members):
                    destination = Path(tmp) / str(index)
                    with self.assertRaises(ValueError):
                        extract_package(self.archive([entry, *members]), destination)
                    self.assertFalse(destination.exists())

    def test_entrypoint_requires_executable_utf8_lf_and_real_gzip(self):
        with tempfile.TemporaryDirectory() as tmp:
            for data, mode in [(b'#!/bin/sh\r\n', 0o755), (b'#!/bin/sh\n', 0o644), (b'\xff', 0o755)]:
                with self.subTest(data=data, mode=mode), self.assertRaises(ValueError):
                    extract_package(io.BytesIO(pack({'patch.sh': (data, mode)})), Path(tmp) / 'bad')
                self.assertFalse((Path(tmp) / 'bad').exists())
            with self.assertRaisesRegex(ValueError, 'gzip'):
                read_package(io.BytesIO(b'PK\x03\x04not a tar'))

    def test_tar_dot_layout_preserves_actual_entry_and_payload(self):
        members = [('.', tarfile.DIRTYPE, b'', 0o755),
                   ('./update', tarfile.DIRTYPE, b'', 0o755),
                   ('./update/update.sh', tarfile.REGTYPE, b'#!/bin/sh\nexit 0\n', 0o755),
                   ('./update/files/app.py', tarfile.REGTYPE, b'print("fixed")\n', 0o644)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'package'
            entry = extract_package(self.archive(members), root)
            self.assertEqual(entry, root / 'update/update.sh')
            self.assertEqual(entry.stat().st_mode & 0o777, 0o755)
            self.assertEqual((root / 'update/files/app.py').read_bytes(), b'print("fixed")\n')
            with self.assertRaises(FileExistsError):
                extract_package(self.archive(members), root)


if __name__ == '__main__':
    unittest.main()
