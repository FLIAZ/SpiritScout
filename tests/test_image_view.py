"""图像快照边界、在线读取与真实 SDK/本机协议传输；不调用真实模型。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from spiritscout.config.settings import GatewayConfig, ModelEntry
from spiritscout.gateway.gateway import ModelGateway
from spiritscout.gateway.registry import ModelRegistry
from spiritscout.tools.images import read_image, view_image
from spiritscout.web.file_routes import mount_artifact_routes

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jY1kAAAAASUVORK5CYII=')


class ImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'attachments').mkdir()
        (self.root / 'attachments' / '像素.png').write_bytes(PNG)
        self.gateway = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content='测试像素', model='fixture')))

    async def load(self, path='attachments/像素.png', gateway=None):
        return await view_image(path, workspace_root=str(self.root), _image_gateway=gateway or self.gateway)

    async def test_model_receives_exact_bytes_and_audit_metadata_has_no_base64(self):
        result = await self.load()
        self.assertTrue(result.ok, result.error)
        image = result.parsed['images'][0]
        self.assertEqual(image['sha256'], hashlib.sha256(PNG).hexdigest())
        self.assertEqual((self.root / image['path']).read_bytes(), PNG)
        args = self.gateway.chat.call_args.args[0]
        self.assertEqual(args[1]['content'][1]['image_url']['url'], 'data:image/png;base64,' + base64.b64encode(PNG).decode())
        self.assertNotIn('base64', json.dumps(result.parsed))
        (self.root / 'attachments' / '像素.png').write_bytes(b'changed')
        self.assertEqual((self.root / image['path']).read_bytes(), PNG)

    async def test_symlinks_traversal_text_and_cross_session_paths_are_rejected(self):
        (self.root / 'attachments' / 'link.png').symlink_to(self.root / 'attachments' / '像素.png')
        (self.root / 'attachments' / 'text.png').write_text('<svg onload="evil"/>')
        for path in ['attachments/link.png', 'attachments/text.png', '../secret.png', '/etc/passwd']:
            with self.subTest(path=path):
                result = await self.load(path)
                self.assertFalse(result.ok)
        self.gateway.chat.assert_not_awaited()

    async def test_model_failure_preserves_preview_and_does_not_report_success(self):
        self.gateway.chat.side_effect = ValueError('model does not support images')
        result = await self.load()
        self.assertFalse(result.ok)
        self.assertEqual(len(result.parsed['images']), 1)
        self.assertIn('does not support images', result.error)

    async def test_scoped_workdir_image_can_be_read(self):
        (self.root / 'sandbox' / 'one').mkdir(parents=True)
        (self.root / 'sandbox' / 'one' / 'screen.png').write_bytes(PNG)
        self.assertEqual(read_image(self.root, 'screen.png', 'one'), PNG)

    async def test_inline_route_checks_session_digest_and_raster_type(self):
        image = (await self.load()).parsed['images'][0]
        app = FastAPI()
        def require(session_id):
            from fastapi import HTTPException
            if session_id != 'one':
                raise HTTPException(404)
            return SimpleNamespace(workspace=self.root)
        mount_artifact_routes(app, require)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
            url='/api/sessions/one/artifacts/image'
            response=await client.get(url, params={'path':image['path'],'sha256':image['sha256']})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['content-type'], 'image/png')
            self.assertEqual(response.content, PNG)
            self.assertEqual((await client.get(url, params={'path':'../private','sha256':image['sha256']})).status_code,404)
            self.assertEqual((await client.get(url.replace('/one/','/other/'),params={'path':image['path'],'sha256':image['sha256']})).status_code,404)
            (self.root / image['path']).write_bytes(PNG + b'changed')
            self.assertEqual((await client.get(url,params={'path':image['path'],'sha256':image['sha256']})).status_code,404)

    @patch.dict('os.environ', {'LITELLM_LOCAL_MODEL_COST_MAP': 'True'})
    async def test_actual_litellm_and_ollama_http_payloads_preserve_image_input(self):
        requests = []
        async def handle(reader, writer):
            try:
                header=await reader.readuntil(b'\r\n\r\n')
                size=next(int(line.split(b':',1)[1]) for line in header.split(b'\r\n')
                          if line.lower().startswith(b'content-length:'))
                body=json.loads(await reader.readexactly(size))
                requests.append(body)
                if b'/api/chat' in header.split(b'\r\n')[0]:
                    result={'model':'fixture','done':True,'message':{'content':'测试像素'}}
                elif b'/responses' in header.split(b'\r\n')[0]:
                    result={'id':'resp-1','object':'response','created_at':1,'status':'completed','model':'fixture',
                        'output':[{'type':'message','id':'msg-1','role':'assistant','status':'completed',
                                   'content':[{'type':'output_text','text':'测试像素','annotations':[]}]}],
                        'usage':{'input_tokens':10,'output_tokens':3,'total_tokens':13}}
                else:
                    result={'id':'chat-1','object':'chat.completion','created':1,'model':'fixture',
                        'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'测试像素'}}],
                        'usage':{'prompt_tokens':10,'completion_tokens':3,'total_tokens':13}}
                data=json.dumps(result).encode()
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '
                    +str(len(data)).encode()+b'\r\nConnection: close\r\n\r\n'+data)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server=await asyncio.start_server(handle,'127.0.0.1',0)
        try:
            port=server.sockets[0].getsockname()[1]
            for provider,fmt in [('openai_compatible','chat_completions'),('openai_compatible','responses'),('ollama','auto')]:
                with self.subTest(provider=provider,fmt=fmt):
                    entry=ModelEntry(model='fixture',provider=provider,api_format=fmt,
                        base_url=f'http://127.0.0.1:{port}' + ('' if provider=='ollama' else '/v1'),api_key='fixture-key')
                    gateway=ModelGateway(ModelRegistry([entry]),config=GatewayConfig(max_retries=0))
                    result=await self.load(gateway=gateway)
                    self.assertTrue(result.ok,result.error)
            encoded=base64.b64encode(PNG).decode()
            self.assertEqual(requests[0]['messages'][1]['content'][1]['image_url']['url'],'data:image/png;base64,'+encoded)
            self.assertEqual(requests[1]['input'][0]['content'][1]['image_url'],'data:image/png;base64,'+encoded)
            self.assertEqual(requests[1]['input'][0]['content'][1]['type'],'input_image')
            self.assertEqual(requests[2]['messages'][1]['images'],[encoded])
            self.assertIsInstance(requests[2]['messages'][1]['content'],str)
        finally:
            server.close()
            await server.wait_closed()


if __name__ == '__main__':
    unittest.main()
