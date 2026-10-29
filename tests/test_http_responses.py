"""真实本机 HTTP 响应落盘、完整性、原文定位与复用，不调用模型。"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from spiritscout.core.models import Scope
from spiritscout.ctf.agents import _http_observation, _obs_text
from spiritscout.policy.scope import ScopeGuard
from spiritscout.tools.builtin import BUILTIN_SPECS, http_probe
from spiritscout.tools.http_responses import http_response_read
from spiritscout.tools.invocation import InvocationContext, current_invocation
from spiritscout.tools.registry import ToolRegistry


class HTTPResponseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.requests = 0
        self.source = ('<html>\r\n' + 'const pad="填充"; ' * 5000
                       + '\r\nconst answer=[17,29,255];\r\n' + 'const pad="填充"; ' * 5000 + '\r\n</html>')
        self.short = '<html>\r\n<script>const hidden=[17,29,255];</script><!--中间线索--></html>'

        async def serve(reader, writer):
            try:
                request = await reader.readuntil(b'\r\n\r\n')
                self.requests += 1
                path = request.split(b' ')[1]
                payload = (self.short if path == b'/short' else self.source).encode()
                if path == b'/invalid':
                    payload = b'const marker="\xff\xc0\xa0";'
                extra = ''
                if path == b'/gzip':
                    payload = gzip.compress(payload)
                    extra = 'Content-Encoding: gzip\r\n'
                status = '206 Partial Content' if path == b'/partial' else '200 OK'
                if path == b'/partial':
                    extra = f'Content-Range: bytes 0-{len(payload)-1}/{len(payload)+1000}\r\n'
                writer.write((f'HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\n'
                              f'{extra}Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n').encode() + payload)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        self.server = await asyncio.start_server(serve, '127.0.0.1', 0)
        self.base = f'http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}'

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        self.temp.cleanup()

    async def fetch(self, path='', **kwargs):
        return await http_probe(self.base + path, guard=ScopeGuard(Scope(local_analysis=True)),
                                workspace_root=str(self.root), **kwargs)

    async def read(self, ref, **kwargs):
        result = await http_response_read(workspace_root=str(self.root), body_ref=ref, **kwargs)
        self.assertTrue(result.ok, result.error)
        return json.loads(result.stdout)

    async def test_long_gzip_saved_in_full_and_local_search_does_not_refetch(self):
        token = current_invocation.set(InvocationContext('inv-http-fixture', 'digest'))
        try:
            result = await self.fetch('/gzip')
        finally:
            current_invocation.reset(token)
        self.assertTrue(result.ok, result.error)
        info = result.parsed
        self.assertTrue(info['download_complete'])
        self.assertFalse(info['body_truncated'])
        self.assertTrue(info['preview_truncated'])
        self.assertNotIn('answer=[17,29,255]', result.stdout)
        body_path = Path(info['body_path'])
        self.assertEqual(body_path.with_name('body.bin').read_bytes(), self.source.encode())
        self.assertEqual(info['body_sha256'], hashlib.sha256(self.source.encode()).hexdigest())
        observation = _http_observation('GET /gzip', result, self.base + '/gzip', [])
        self.assertLess(len(_obs_text(observation)), 5000)
        found = await self.read(info['body_ref'], mode='search', query='answer=[17,29,255]')
        self.assertIn('answer=[17,29,255]', found['matches'][0]['text'])
        self.assertEqual(found['source_invocation_id'], 'inv-http-fixture')
        offset = found['matches'][0]['offset']
        exact = await self.read(info['body_ref'], offset=offset, limit=24)
        self.assertEqual(exact['text'], self.source[offset:offset + 24])
        line = await self.read(info['body_ref'], start_line=3)
        line_start = self.source.index('const answer=')
        self.assertEqual(line['text'], self.source[line_start:line_start + 3500])
        self.assertEqual(self.requests, 1)

    async def test_short_html_reaches_ctf_without_stripping_script_or_comments(self):
        result = await self.fetch('/short')
        obs = _http_observation('GET /short', result, self.base + '/short', [])
        self.assertEqual(obs['excerpt'], self.short)
        self.assertFalse(obs['preview_truncated'])
        self.assertIn('body_ref=', _obs_text(obs))
        self.assertIn(self.short, _obs_text(obs))

    async def test_download_cap_is_explicit_and_independent_of_preview(self):
        result = await self.fetch('/gzip', http_max_body_bytes=128)
        self.assertTrue(result.ok, result.error)
        self.assertFalse(result.parsed['download_complete'])
        self.assertTrue(result.parsed['body_truncated'])
        self.assertFalse(result.parsed['preview_truncated'])
        self.assertEqual(Path(result.parsed['body_path']).with_name('body.bin').stat().st_size, 128)
        obs = _http_observation('GET /gzip', result, self.base + '/gzip', [])
        self.assertIn('下载不完整', _obs_text(obs))
        self.assertNotIn('以下为完整正文', _obs_text(obs))
        self.assertFalse((await self.read(result.parsed['body_ref']))['download_complete'])

    async def test_contiguous_reads_reconstruct_exact_source_including_crlf(self):
        result = await self.fetch('/short')
        ref = result.parsed['body_ref']
        offset, parts = 0, []
        while True:
            chunk = await self.read(ref, offset=offset, limit=7)
            parts.append(chunk['text'])
            offset = chunk['next_offset']
            if not chunk['has_more']:
                break
        self.assertEqual(''.join(parts), self.short)
        self.assertEqual(self.requests, 1)

    async def test_references_are_session_local_and_cannot_traverse(self):
        result = await self.fetch('/short')
        ref = result.parsed['body_ref']
        other = await http_response_read(workspace_root=str(self.root / 'other'), body_ref=ref)
        self.assertFalse(other.ok)
        escaped = await http_response_read(workspace_root=str(self.root), body_ref='../execution.key')
        self.assertFalse(escaped.ok)
        bad = await http_response_read(workspace_root=str(self.root), body_ref=ref, offset=-1)
        self.assertFalse(bad.ok)
        folder = self.root / 'http-responses' / ref
        (folder / 'body.txt').unlink()
        (folder / 'body.txt').symlink_to(self.root / 'outside.txt')
        self.assertFalse((await http_response_read(workspace_root=str(self.root), body_ref=ref)).ok)

    async def test_cached_http_reuses_same_immutable_snapshot(self):
        registry = ToolRegistry(SimpleNamespace(), max_retries=0)
        registry.register_builtin('http_probe', http_probe, BUILTIN_SPECS['http_probe'])
        context = {'guard': ScopeGuard(Scope(local_analysis=True)), 'workspace_root': str(self.root)}
        first = await registry.call('http_probe', {'url': self.base + '/short'}, **context)
        second = await registry.call('http_probe', {'url': self.base + '/short'}, **context)
        self.assertTrue(first.ok, first.error)
        self.assertTrue(second.ok, second.error)
        self.assertTrue(second.reuse)
        self.assertEqual(first.parsed['body_ref'], second.parsed['body_ref'])
        saved = await self.read(second.parsed['body_ref'])
        self.assertEqual(saved['text'], self.short)
        self.assertEqual(self.requests, 1)

    async def test_partial_http_response_is_not_claimed_to_be_entire_resource(self):
        result = await self.fetch('/partial')
        self.assertTrue(result.parsed['download_complete'])
        self.assertTrue(result.parsed['response_is_partial'])
        obs = _http_observation('GET /partial', result, self.base + '/partial', [])
        self.assertIn('不代表完整资源', _obs_text(obs))

    async def test_invalid_text_encoding_keeps_original_bytes_and_reports_loss(self):
        result = await self.fetch('/invalid')
        self.assertTrue(result.parsed['text_decode_lossy'])
        self.assertEqual(Path(result.parsed['body_path']).with_name('body.bin').read_bytes(), b'const marker="\xff\xc0\xa0";')
        self.assertTrue((await self.read(result.parsed['body_ref']))['text_decode_lossy'])
