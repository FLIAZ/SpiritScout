"""OneForAll 被动子域情报适配器。

仅运行 OneForAll 的多源收集模块，关闭爆破、DNS 解析、HTTP 请求和接管检查。
输出永远进入外部候选层，不直接授予主动测试资格。
"""

from __future__ import annotations

import asyncio
import codecs
import json
import os
import signal
import sys
from pathlib import Path

from spiritscout.assets.providers.base import (
    OutputCallback,
    ProviderCandidate,
    ProviderResult,
)


class OneForAllProvider:
    name = "oneforall"

    def __init__(self, root: str, *, python: str = "", timeout_seconds: int = 600) -> None:
        self.root = Path(root).expanduser().resolve()
        self.python = python or sys.executable
        self.timeout_seconds = max(30, int(timeout_seconds))

    async def collect(self, domain: str, *, brute: bool = False,
                      on_output: OutputCallback | None = None) -> ProviderResult:
        if not self.root.is_dir() or not (self.root / "oneforall.py").is_file():
            return ProviderResult(self.name, "not_configured", error="oneforall_path_invalid")
        script = (
            "import json, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from oneforall import OneForAll\n"
            f"data = OneForAll(target=sys.argv[2], brute={'True' if brute else 'False'}, "
            f"dns={'True' if brute else 'False'}, req=False, "
            "takeover=False, fmt='json').run()\n"
            "print('__SPIRITSCOUT_JSON__' + json.dumps(data, ensure_ascii=False))\n"
        )
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                self.python, "-c", script, str(self.root), domain,
                cwd=str(self.root), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                start_new_session=os.name == "posix",
            )
            async def read_output(stream: asyncio.StreamReader, channel: str) -> bytes:
                chunks: list[bytes] = []
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                pending = ""
                json_line = False
                while chunk := await stream.read(4096):
                    chunks.append(chunk)
                    pending += decoder.decode(chunk)
                    while "\n" in pending:
                        line, pending = pending.split("\n", 1)
                        if on_output and not json_line and not line.startswith("__SPIRITSCOUT_JSON__"):
                            on_output(f"{channel}: {line.rstrip()}")
                        json_line = False
                    if pending.startswith("__SPIRITSCOUT_JSON__"):
                        json_line = True
                    if len(pending) >= 4096:
                        if on_output and not json_line:
                            on_output(f"{channel}: {pending}")
                        pending = ""
                pending += decoder.decode(b"", final=True)
                if pending and on_output and not json_line and not pending.startswith("__SPIRITSCOUT_JSON__"):
                    on_output(f"{channel}: {pending.rstrip()}")
                return b"".join(chunks)

            async def communicate() -> tuple[bytes, bytes]:
                stdout, stderr = await asyncio.gather(
                    read_output(process.stdout, "stdout"), read_output(process.stderr, "stderr"))
                await process.wait()
                return stdout, stderr

            stdout, stderr = await asyncio.wait_for(communicate(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            return ProviderResult(self.name, "failed", error="timeout")
        except (OSError, ValueError) as exc:
            return ProviderResult(self.name, "failed", error=type(exc).__name__)
        finally:
            if process is not None:
                # 取消收集必须回收实际进程；Linux 上包括收集器派生的进程。
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    elif process.returncode is None:
                        process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        if process.returncode != 0:
            return ProviderResult(self.name, "failed", error=(stderr.decode(errors="replace")[-500:]
                                                               or "process_failed"))
        try:
            output = stdout.decode(errors="replace")
            marker = "__SPIRITSCOUT_JSON__"
            payload = output.rsplit(marker, 1)[1].lstrip()
            raw, _ = json.JSONDecoder().raw_decode(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, IndexError):
            return ProviderResult(self.name, "failed", error="invalid_json_output")
        rows = raw if isinstance(raw, list) else []
        candidates: list[ProviderCandidate] = []
        seen: set[str] = set()
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            value = str(row.get("subdomain") or row.get("host") or "").strip().lower().rstrip(".")
            if not value or value in seen:
                continue
            seen.add(value)
            candidates.append(ProviderCandidate(
                value=value,
                source_ref=f"oneforall:passive:row-{index + 1}",
                observation={
                    "source": "oneforall",
                    "verification_status": "passive",
                    "scope_status": "candidate_only",
                    "related_domain": domain,
                    "ip": str(row.get("ip") or "")[:120],
                    "cname": str(row.get("cname") or "")[:300],
                    "url": str(row.get("url") or "")[:500],
                    "title": str(row.get("title") or "")[:300],
                    "status": str(row.get("status") or "")[:40],
                    "module": str(row.get("module") or "")[:200],
                    "source_detail": str(row.get("source") or "")[:300],
                },
            ))
        return ProviderResult(self.name, "ok", candidates=candidates, returned=len(rows),
                              truncated=len(rows) > len(candidates))
