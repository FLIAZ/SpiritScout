"""显式真实测试专用的费用计量；不接入生产网关或业务预算。

每次物理请求先预留最高单价费用；在途请求释放预留前等待，不因并发占用
提前取消。仅保存用量、哈希和标识，不保存请求、密钥或模型正文。
"""
from __future__ import annotations

import asyncio
import json
import math
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx


class TestBudgetExceeded(asyncio.CancelledError):
    """隔离测试费用不足；不可被网关的普通异常重试吞掉。"""


class LiveBudget:
    def __init__(self, path: Path, *, limit_cny: float = 1,
                 previous_cost_cny: float = 0, off_peak_only: bool = False) -> None:
        self.path, self.limit_cny, self.previous_cost_cny = path, limit_cny, previous_cost_cny
        if not (math.isfinite(limit_cny) and math.isfinite(previous_cost_cny)
                and limit_cny>0 and 0<=previous_cost_cny<=limit_cny):
            raise ValueError('测试额度和历史费用必须是有效非负金额，且历史费用不得超过额度')
        self.off_peak_only=off_peak_only
        self.rows: list[dict[str, Any]] = []
        self._changed = asyncio.Event()
        if path.exists():
            saved = json.loads(path.read_text())
            if (saved['limit_cny'],saved['previous_cost_cny']) != (limit_cny,previous_cost_cny):
                raise ValueError('已有测试账本的预算和历史费用不能被覆盖')
            if saved.get('off_peak_only',False)!=off_peak_only:
                raise ValueError('已有测试账本的定价策略不能被覆盖')
            self.rows = saved['requests']
            for row in self.rows:
                if row['status']=='pending':
                    row['status']='unknown'
        self._save()

    @property
    def committed_cny(self) -> float:
        return self.previous_cost_cny + sum(row.get('actual_upper_cny',row['reserve_cny']) for row in self.rows)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True,exist_ok=True)
        pending=self.path.with_suffix('.tmp')
        pending.write_text(json.dumps({'limit_cny':self.limit_cny,'previous_cost_cny':self.previous_cost_cny,
            'off_peak_only':self.off_peak_only,
            'committed_upper_cny':self.committed_cny,'requests':self.rows},ensure_ascii=False,indent=2))
        pending.replace(self.path)

    def _notify(self) -> None:
        self._changed.set()
        self._changed=asyncio.Event()
        self._save()

    async def reserve(self, attempt_id: str, body: bytes, max_tokens: int) -> str:
        if type(max_tokens) is not int or max_tokens<=0:
            raise ValueError('真实测试必须预先声明单次输出上限')
        # SDK 内部重试可能尚未触发外层 attempt_finished；不能等待自己的旧请求。
        for row in self.rows:
            if attempt_id and row['attempt_id']==attempt_id and row['status']=='pending':
                self.unknown(row['id'])
        # UTF-8 字节数及协议余量作为输入上界；实际收费使用供应商 usage。
        def price_scale() -> float:
            if not self.off_peak_only:
                return 1
            now=datetime.now(ZoneInfo('Asia/Shanghai'))
            # 临近高峰前 20 分钟也不启动；不依赖节假日判断，宁可多预留。
            for point in (now,now+timedelta(minutes=20)):
                if point.weekday()<5 and (9<=point.hour<12 or 14<=point.hour<18):
                    raise TestBudgetExceeded('仅空闲时段测试：当前或即将进入高峰，不发起请求')
            return .5
        reserve=((len(body)+2048)*2+max_tokens*8)/1e6*price_scale()
        while self.committed_cny+reserve>self.limit_cny:
            changed=self._changed
            if not any(row['status']=='pending' for row in self.rows):
                raise TestBudgetExceeded('剩余测试额度不足以覆盖下一次请求')
            await changed.wait()
            reserve=((len(body)+2048)*2+max_tokens*8)/1e6*price_scale()
        key=uuid.uuid4().hex
        self.rows.append({'id':key,'attempt_id':attempt_id,'reserve_cny':reserve,'status':'pending',
                          'price_scale':.5 if self.off_peak_only else 1})
        self._save()
        return key

    def usage(self, key: str, usage: Any) -> None:
        if not isinstance(usage,dict):
            return
        inp,out=usage.get('prompt_tokens'),usage.get('completion_tokens')
        if type(inp) is not int or type(out) is not int or min(inp,out)<0:
            return
        row=next(item for item in self.rows if item['id']==key)
        cached=usage.get('prompt_cache_hit_tokens')
        if cached is None:
            cached=(usage.get('prompt_tokens_details') or {}).get('cached_tokens')
        if type(cached) is not int or not 0<=cached<=inp:
            cached=None
        row.update(input_tokens=inp,output_tokens=out,cached_input_tokens=cached,
                   actual_upper_cny=((inp-(cached or 0))*2+(cached or 0)*.04+out*8)/1e6*row.get('price_scale',1),
                   status='reported')
        self._notify()

    def unknown(self, key: str) -> None:
        row=next(item for item in self.rows if item['id']==key)
        if row['status']=='pending':
            row['status']='unknown'
            self._notify()


class _MeasuredStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, budget: LiveBudget, key: str, *,
                 sse: bool, audit_pending: bool = False):
        self.stream,self.budget,self.key,self.sse=stream,budget,key,sse
        self.audit_pending=audit_pending
        self.buffer=b''

    async def __aiter__(self):
        try:
            async for chunk in self.stream:
                self.buffer+=chunk
                if self.sse:
                    while b'\n' in self.buffer:
                        line,self.buffer=self.buffer.split(b'\n',1)
                        if line.startswith(b'data:'):
                            self._parse(line[5:].strip())
                yield chunk
        except BaseException:
            self.budget.unknown(self.key)
            raise
        if not self.sse:
            self._parse(self.buffer)

    def _parse(self, body: bytes) -> None:
        try:
            value=json.loads(body)
            if isinstance(value,dict):
                self.budget.usage(self.key,value.get('usage'))
        except ValueError:
            pass

    async def aclose(self) -> None:
        try:
            await self.stream.aclose()
        finally:
            if not self.audit_pending:
                self.budget.unknown(self.key)


class LiveMeter:
    """仅在独立 benchmark 进程安装；退出时恢复 SDK/HTTP/审计方法。"""

    def __init__(self, budget: LiveBudget, *, max_tokens: int = 8192):
        self.budget,self.max_tokens=budget,max_tokens

    def __enter__(self):
        import litellm

        from spiritscout.core.audit import AuditLogger
        from spiritscout.gateway.attempts import current_attempt_trace

        self._send,self._log,self._completion=httpx.AsyncClient.send,AuditLogger.log,litellm.acompletion

        async def send(client,request,**kwargs):
            if request.url.host!='api.deepseek.com' or not request.url.path.endswith('/chat/completions'):
                raise TestBudgetExceeded('独立测试仅连接已授权的 DeepSeek 聊天端点')
            payload=json.loads(request.content)
            if payload.get('model')!='deepseek-flash' or payload.get('max_tokens')!=self.max_tokens:
                raise TestBudgetExceeded('测试模型或输出上限不符合对照约定')
            trace=current_attempt_trace.get()
            attempt_id=trace.attempts[-1]['attempt_id'] if trace and trace.attempts else ''
            key=await self.budget.reserve(attempt_id,request.content,self.max_tokens)
            try:
                response=await self._send(client,request,**kwargs)
                if response.is_stream_consumed:
                    parser=_MeasuredStream(response.stream,self.budget,key,sse=False)
                    if 'text/event-stream' in response.headers.get('content-type',''):
                        for line in response.content.splitlines():
                            if line.startswith(b'data:'):
                                parser._parse(line[5:].strip())
                    else:
                        parser._parse(response.content)
                    self.budget.unknown(key)
                else:
                    response.stream=_MeasuredStream(response.stream,self.budget,key,
                        sse='text/event-stream' in response.headers.get('content-type',''),
                        audit_pending=bool(attempt_id))
                return response
            except BaseException:
                self.budget.unknown(key)
                raise

        def log(logger,event_type,actor,detail):
            result=self._log(logger,event_type,actor,detail)
            if event_type=='model.attempt_finished':
                matches=[row for row in self.budget.rows if row['attempt_id']==detail.get('attempt_id')]
                if len(matches)==1:
                    self.budget.usage(matches[0]['id'],{'prompt_tokens':detail.get('input_tokens'),
                        'completion_tokens':detail.get('output_tokens'),
                        'prompt_cache_hit_tokens':detail.get('cached_input_tokens')})
                    self.budget.unknown(matches[0]['id'])
            return result

        async def completion(**kwargs):
            kwargs.update(max_tokens=self.max_tokens,num_retries=0,max_retries=0)
            return await self._completion(**kwargs)

        httpx.AsyncClient.send,AuditLogger.log,litellm.acompletion=send,log,completion
        return self

    def __exit__(self,*args):
        import litellm

        from spiritscout.core.audit import AuditLogger

        httpx.AsyncClient.send,AuditLogger.log,litellm.acompletion=self._send,self._log,self._completion
