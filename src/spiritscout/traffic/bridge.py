"""私有 Unix socket 到会话录制网关的桥接；不提供任意目标拨号能力。"""
from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


class EgressBridge:
    def __init__(self, address):
        self.address = urlsplit(address)
        self.directory = tempfile.TemporaryDirectory(prefix='ss-egress-')
        self.path = str(Path(self.directory.name) / 'gateway.sock')
        self.server = None
        self.tasks = set()

    async def start(self):
        self.server = await asyncio.start_unix_server(self.accept, path=self.path)
        os.chmod(self.path, 0o600)

    async def accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        remote = None
        async def copy(source, destination):
            while chunk := await source.read(65536):
                destination.write(chunk)
                await destination.drain()
            if destination.can_write_eof():
                destination.write_eof()
        try:
            incoming, remote = await asyncio.open_connection(self.address.hostname, self.address.port)
            await asyncio.gather(copy(reader, remote), copy(incoming, writer))
        except (OSError, ConnectionError):
            pass
        finally:
            for stream in (writer, remote):
                if stream:
                    stream.close()
                    with contextlib.suppress(OSError):
                        await stream.wait_closed()
            self.tasks.discard(task)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        self.directory.cleanup()
