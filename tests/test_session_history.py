"""长会话历史按字节分页，首次加载和 SSE 续传不需要全量回放。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spiritscout.web.events import read_audit_page, stream_audit


def line(index: int) -> bytes:
    return (json.dumps({'type': 'master.reply', 'actor': 'master',
                        'detail': {'text': f'中文记录 {index}'}, 'seq': index},
                       ensure_ascii=False) + '\n').encode()


class AuditHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_pages_cover_unicode_history_without_gaps_or_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'audit.jsonl'
            path.write_bytes(b''.join(line(i) for i in range(1300)))
            before = None
            collected = []
            while True:
                page = read_audit_page(path, before=before, limit=160)
                self.assertLessEqual(len(page['items']), 160)
                self.assertEqual(sorted(x['id'] for x in page['items']),
                                 [x['id'] for x in page['items']])
                collected = [x['event']['seq'] for x in page['items']] + collected
                if not page['has_more']:
                    break
                self.assertNotEqual(before, page['older_cursor'])
                before = page['older_cursor']
            self.assertEqual(collected, list(range(1300)))

    async def test_new_frames_start_at_exact_page_byte_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'audit.jsonl'
            path.write_bytes(line(1) + line(2))
            page = read_audit_page(path)
            with path.open('ab') as fh:
                fh.write(line(3))
            frames = [f async for f in stream_audit(path, byte_offset=page['cursor'],
                       poll=0, is_terminal=lambda: True)]
            audits = [f for f in frames if f.startswith('id:')]
            self.assertEqual(len(audits), 1)
            self.assertIn('"seq": 3', audits[0])
            self.assertTrue(audits[0].startswith(f'id: {path.stat().st_size}\n'))
            self.assertIn('event: done', frames[-1])

    async def test_partial_tail_is_not_lost_between_page_and_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'audit.jsonl'
            partial = line(2)
            split = len(partial) // 2
            path.write_bytes(line(1) + partial[:split])
            page = read_audit_page(path)
            self.assertEqual([x['event']['seq'] for x in page['items']], [1])
            self.assertEqual(page['cursor'], len(line(1)))
            with path.open('ab') as fh:
                fh.write(partial[split:])
            frames = [f async for f in stream_audit(path, byte_offset=page['cursor'],
                       poll=0, max_frames=1)]
            self.assertIn('"seq": 2', frames[0])

    async def test_missing_empty_invalid_blank_and_long_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'audit.jsonl'
            self.assertEqual(read_audit_page(path)['cursor'], 0)
            path.write_bytes(b'')
            self.assertEqual(read_audit_page(path)['items'], [])
            huge = json.dumps({'type': 'master.reply', 'detail': {'text': '长' * 90000}},
                              ensure_ascii=False).encode() + b'\n'
            path.write_bytes(line(1) + b'\ninvalid\n' + huge + line(2))
            page = read_audit_page(path, limit=2)
            self.assertEqual(page['items'][0]['event']['detail']['text'], '长' * 90000)
            older = read_audit_page(path, before=page['older_cursor'], limit=2)
            self.assertEqual(older['items'][-1]['event']['type'], 'audit.invalid')
            self.assertEqual(older['cursor'], page['older_cursor'])

    async def test_legacy_line_cursor_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'audit.jsonl'
            path.write_bytes(line(1) + line(2))
            frames = [f async for f in stream_audit(path, 1, poll=0, max_frames=1)]
            self.assertTrue(frames[0].startswith('id: 2\n'))
            self.assertIn('"seq": 2', frames[0])

    async def test_recent_page_only_reads_tail_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'audit.jsonl'
            path.write_bytes(line(1) * 30000)
            with path.open('rb') as raw:
                class TrackedReader:
                    consumed = 0

                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        return False

                    def seek(self, *args):
                        return raw.seek(*args)

                    def read(self, size=-1):
                        data = raw.read(size)
                        self.consumed += len(data)
                        return data

                tracked = TrackedReader()
                with patch('spiritscout.web.events.Path.open', return_value=tracked):
                    page = read_audit_page(path, limit=5)
                self.assertEqual(len(page['items']), 5)
                self.assertLessEqual(tracked.consumed, 65536)
                self.assertTrue(page['has_more'])
