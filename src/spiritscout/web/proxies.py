"""独立代理池 API；凭据输入不经 Pydantic 错误回显。"""
from __future__ import annotations

import json

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.resources.proxy_pool import (
    MAX_IMPORT_BYTES,
    ProxyPool,
    SubscriptionContentError,
)


def mount_proxies(app, store, manager=None):
    from spiritscout.traffic.benchmark import ProxyBenchmarks

    pool = ProxyPool(store)
    benchmarks = ProxyBenchmarks(store)

    @app.get('/api/proxies/benchmark')
    def benchmark_status():
        return JSONResponse(benchmarks.view(), headers={'Cache-Control': 'no-store'})

    @app.post('/api/proxies/benchmark/cancel')
    async def benchmark_cancel():
        await benchmarks.close()
        return JSONResponse(benchmarks.view(), headers={'Cache-Control': 'no-store'})
    if manager is not None:
        @app.post('/api/sessions/{session_id}/secrets/{reference}/reveal')
        async def reveal(session_id: str, reference: str, request: Request):
            from spiritscout.core.audit import AuditLogger
            from spiritscout.resources.session_secrets import SessionSecrets

            session = manager.get(session_id)
            if session is None:
                raise HTTPException(404, '会话不存在')
            if not reference.startswith('secret-') or len(reference) != 19:
                raise HTTPException(422, '秘密引用无效')
            try:
                content = bytearray()
                async for chunk in request.stream():
                    content.extend(chunk)
                    if len(content) > 8192:
                        raise ValueError('请求过大')
                payload = json.loads(content)
                if not isinstance(payload, dict) or payload.get('confirm') is not True:
                    raise ValueError('需要显式确认查看明文')
                value = SessionSecrets(session.workspace).resolve(reference, payload['target'])
            except (ValueError, TypeError, KeyError):
                raise HTTPException(422, '引用或目标无效，需要显式确认查看') from None
            AuditLogger(session.audit_path).log('secret.revealed', 'user', {'secret_ref': reference})
            return JSONResponse({'value': value}, headers={'Cache-Control': 'no-store'})

    @app.get("/api/proxies")
    def listing():
        return JSONResponse(pool.view(), headers={"Cache-Control": "no-store"})

    @app.post("/api/proxies/{operation}")
    async def mutate(operation: str, request: Request):
        try:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_IMPORT_BYTES + 65536:
                    raise ValueError("请求内容超过上限")
            payload = json.loads(body)
            if not isinstance(payload, dict) or type(payload.get("version")) is not int:
                raise ValueError("请提供代理池版本")
            version = payload["version"]
            if operation == 'benchmark':
                result = benchmarks.start(payload['nodes'], payload['mode'], version)
            elif operation == "import":
                result = pool.import_content(payload["content"], version)
            elif operation == "plan":
                result = pool.save_plan(payload["name"], payload["nodes"], version)
            elif operation == "subscribe":
                result = await pool.subscribe(payload["name"], payload["url"], version)
            elif operation == "refresh":
                result = await pool.refresh(payload["id"], version)
            elif operation == "delete":
                result = pool.delete(payload["kind"], payload["id"], version)
            else:
                raise HTTPException(404, "操作不存在")
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except SpiritScoutError as exc:
            raise HTTPException(409, str(exc)) from None
        except SubscriptionContentError as exc:
            raise HTTPException(422, str(exc)) from None
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise HTTPException(422, "代理配置无效，请检查格式、节点参数及版本；订阅仅支持公网 HTTPS 地址") from None
    return benchmarks
