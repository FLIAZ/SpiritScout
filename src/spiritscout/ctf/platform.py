"""平台档案与对接适配器：主办方托管平台（跑分/CTF）的通用对接格式。

平台档案是一份声明式配置（YAML/JSON）——接口名单（列题 / 开实例 /
关实例 / 提交 / 提示）、挑战字段映射、并发上限与固定请求头。换主办方
= 换一份档案，代码不动：档案由会话装配层从任务文本抽取或由用户显式
提供（LLM 读平台 API 文档产出，草稿即用、失败降级不阻断）。

边界（与「子 Agent 不接触平台」一致）：
- 平台凭据（请求头）只在本适配器内使用，审计与落盘一律打码；
- 所有平台操作由主 Agent（灵枢）侧调用：列题在会话装配期、开实例在
  调度期、关实例在完成回收期、Flag 提交经 FlagManager 的提交回调；
- 题面事实经 request 的字段映射展开注入（点路径 + 数组取首元素），
  不写死任何主办方字段名。

机械语义与模型语义的分界：id / 容器地址 / 完成态这类「接线用」字段由
档案映射后机械使用；description、difficulty 等其余映射值只作为题面
事实随挑战载荷下发，由解题 Agent 自行理解。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from spiritscout.core.audit import AuditLogger, NullAuditLogger
from spiritscout.ctf.flags import SubmitResult

try:  # PyYAML 为项目既有依赖；缺失时仅显式 YAML 文本路径不可用
    import yaml
except ImportError:  # pragma: no cover —— 常规环境始终可用
    yaml = None  # type: ignore[assignment]

_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_KNOWN_OPS = ("list", "start", "close", "submit", "hint", "precheck")
_SIZE_ERRORS = (ValueError, TypeError, KeyError, AttributeError)


# ---------------------------------------------------------------- 点路径工具


def _dig(data: Any, path: str) -> Any:
    """按点路径取值：``a.b.c``；段为数字时索引数组；中途取不到回 None。"""
    if not path:
        return None
    node = data
    for segment in str(path).split("."):
        if isinstance(node, dict):
            if segment not in node:
                return None
            node = node[segment]
        elif isinstance(node, list):
            if not segment.isdigit() or int(segment) >= len(node):
                return None
            node = node[int(segment)]
        else:
            return None
    return node


def _first_scalar(value: Any) -> str:
    """归一为字符串：数组取第一个非空标量；字典/空值 → ""。"""
    if isinstance(value, list):
        for item in value:
            got = _first_scalar(item)
            if got:
                return got
        return ""
    if value is None or isinstance(value, (dict, bool)):
        return ""
    return str(value).strip()


def _as_str(value: Any) -> str:
    got = _first_scalar(value)
    return got


def _maybe_url(addr: str) -> str:
    """容器地址归一为可请求 URL：缺 scheme 时按 http 补齐（模型可自行改）。"""
    addr = addr.strip()
    if not addr:
        return ""
    if "://" in addr:
        return addr
    return f"http://{addr}"


def _render(value: Any, ctx: dict[str, str]) -> Any:
    """把 ``{challenge}`` / ``{flag}`` 占位符渲染进字符串（递归容器）。"""
    if isinstance(value, str):
        return _PLACEHOLDER.sub(
            lambda m: str(ctx.get(m.group(1), m.group(0))), value)
    if isinstance(value, dict):
        return {k: _render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, ctx) for v in value]
    return value


# ---------------------------------------------------------------- 档案模型


@dataclass
class PlatformOp:
    """一个平台接口的声明（占位符 ``{challenge}`` / ``{flag}``）。"""

    method: str = ""
    url: str = ""                 # 完整地址（precheck 等跨域端点用）
    path: str = ""                # 相对 base_url 的路径
    query: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    items_field: str = ""         # list：应答中题目数组的点路径（空=自动探测）
    addr_field: str = ""          # start：容器地址字段点路径
    accepted_field: str = ""      # submit：判定字段点路径（correct: true/false）
    duplicate_code: str = ""      # submit：重复提交业务码（幂等→已计分）

    def http_method(self) -> str:
        if self.method:
            return self.method.strip().upper()
        return "POST" if self.body else "GET"

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in {
            "method": self.method, "url": self.url, "path": self.path,
            "query": self.query, "body": self.body,
            "items_field": self.items_field, "addr_field": self.addr_field,
            "accepted_field": self.accepted_field,
            "duplicate_code": self.duplicate_code,
        }.items() if v not in ("", {}, None)}


@dataclass
class PlatformProfile:
    """平台档案：接口名单 + 字段映射 + 并发上限 + 固定请求头。"""

    name: str = "platform"
    base_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    ops: dict[str, PlatformOp] = field(default_factory=dict)
    challenge_fields: dict[str, str] = field(
        default_factory=lambda: {"id": "id", "title": "title"})
    limits: dict[str, Any] = field(default_factory=dict)

    # -------------------------------------------------- 解析

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PlatformProfile:
        """宽松解析：结构不对抛 ValueError（调用方降级为「无档案」）。"""
        if not isinstance(raw, dict):
            raise ValueError("平台档案必须是对象")
        inner = raw.get("platform") if isinstance(raw.get("platform"), dict) else None
        if inner is not None and "ops" not in raw and "base_url" not in raw:
            raw = inner
        name = str(raw.get("name") or "platform").strip()[:80] or "platform"
        base_url = str(raw.get("base_url") or raw.get("base") or "").strip().rstrip("/")
        headers = {str(k): str(v) for k, v in (raw.get("headers") or {}).items()} \
            if isinstance(raw.get("headers"), dict) else {}
        ops: dict[str, PlatformOp] = {}
        raw_ops = raw.get("ops") or raw.get("endpoints")
        if isinstance(raw_ops, dict):
            for key in _KNOWN_OPS:
                value = raw_ops.get(key)
                if value is None:
                    continue
                op = _parse_op(key, value)
                if op is not None:
                    ops[key] = op
        fields: dict[str, Any] = raw.get("challenge_fields") \
            or raw.get("fields") or {}
        if isinstance(fields, dict) and fields:
            challenge_fields = {str(k): str(v) for k, v in fields.items() if v}
        else:
            challenge_fields = {"id": "id", "title": "title"}
        if "id" not in challenge_fields and ops.get("list") is not None:
            raise ValueError("平台档案缺少 challenge_fields.id 映射（列题必须能取到题号）")
        limits = dict(raw.get("limits") or {}) if isinstance(raw.get("limits"), dict) else {}
        return cls(name=name, base_url=base_url, headers=headers, ops=ops,
                   challenge_fields=challenge_fields, limits=limits)

    @classmethod
    def from_yaml(cls, text: str) -> PlatformProfile:
        if yaml is None:  # pragma: no cover
            raise ValueError("当前环境缺少 PyYAML，无法解析平台档案文本")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"平台档案不是合法 YAML: {str(exc)[:200]}") from exc
        return cls.from_dict(data or {})

    def redacted(self) -> dict[str, Any]:
        """可审计形态：请求头值一律打码（凭据不进审计/报告）。"""
        return {
            "name": self.name, "base_url": self.base_url,
            "headers": {k: "***" for k in self.headers},
            "ops": {k: op.to_dict() for k, op in self.ops.items()},
            "challenge_fields": dict(self.challenge_fields),
            "limits": dict(self.limits),
        }

    def to_dict(self) -> dict[str, Any]:
        """完整档案（含凭据）——仅供运行装配；审计/落盘前必须 redacted()。"""
        data = self.redacted()
        data["headers"] = dict(self.headers)
        return data


def _parse_op(key: str, value: Any) -> PlatformOp | None:
    """接口声明解析：字符串=路径简写；字典按字段读；其余忽略。"""
    default_method = "GET" if key in ("list", "hint", "precheck") else "POST"
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.upper().startswith(("GET ", "POST ", "PUT ", "DELETE ")):
            method, _, path = text.partition(" ")
            return PlatformOp(method=method, path=path.strip())
        return PlatformOp(method=default_method,
                          url=text if text.startswith("http") else "",
                          path="" if text.startswith("http") else text)
    if not isinstance(value, dict):
        return None
    op = PlatformOp(
        method=str(value.get("method") or default_method),
        url=str(value.get("url") or ""),
        path=str(value.get("path") or value.get("endpoint") or ""),
        items_field=str(value.get("items_field") or ""),
        addr_field=str(value.get("addr_field") or ""),
        accepted_field=str(value.get("accepted_field") or ""),
        duplicate_code=str(value.get("duplicate_code") or ""),
    )
    for attr in ("query", "body", "params", "json", "data"):
        block = value.get(attr)
        if isinstance(block, dict):
            target = op.body if attr in ("body", "json", "data") or (
                attr == "params" and op.method.upper() != "GET") else op.query
            target.update({str(k): v for k, v in block.items()})
    if not op.url and not op.path:
        return None
    return op


@dataclass
class _Resp:
    ok: bool          # 是否拿到 HTTP 应答（传输层失败 → False）
    status: int
    data: Any
    text: str
    error: str = ""


# ---------------------------------------------------------------- 适配器


class PlatformAdapter:
    """平台适配器：列题 / 开实例（幂等 + 名额） / 关实例 / 提交 / 回收。

    生命周期由会话装配层持有（run_ctf_engagement），主 Agent 通过
    ``ctx.extra["platform"]`` 在调度环里驱动；结束必 aclose。
    """

    _REFRESH_MIN_INTERVAL = 15.0    # 列表节流（秒）：调度环每轮一调不放大请求
    _START_RETRY_BACKOFF = 30.0     # 开实例失败退避：防故障期空转打爆平台

    def __init__(self, profile: PlatformProfile, *,
                 audit: AuditLogger | None = None,
                 facts: list[dict[str, Any]] | None = None,
                 timeout: float = 30.0,
                 progress_path: str | Path | None = None,
                 continuous: bool = False) -> None:
        self.profile = profile
        self.audit = audit or NullAuditLogger()
        self.facts: list[dict[str, Any]] = facts if facts is not None else []
        self.timeout = timeout
        self.progress_path = Path(progress_path) if progress_path else None
        self.continuous = continuous
        self.completion_queue: asyncio.Queue[str] = asyncio.Queue()
        self._progress: dict[str, Any] = {
            "total_challenges": 0, "solved_ids": [], "unsolved_ids": []}
        self._client: httpx.AsyncClient | None = None
        self._instances: dict[str, str] = {}          # 题 id → 容器地址
        self._listed: dict[str, dict[str, Any]] = {}  # 题 id → 映射后的挑战
        self._listed_at = 0.0
        self._start_blocked_until = 0.0
        self.closed = False

    # -------------------------------------------------- 事实与请求

    def _record(self, action: str, **fields: Any) -> None:
        fact = {"action": action, "ts": int(time.time()), **fields}
        self.facts.append(fact)
        del self.facts[:-40]
        self.audit.log("platform.fact", "platform_adapter", dict(fact))

    def _update_progress(self, challenges: list[dict[str, Any]]) -> None:
        """平台完成态 → 会话进度快照；有路径时原子落盘。"""
        solved = sorted(str(ch.get("id") or "") for ch in challenges
                        if ch.get("id") and bool(ch.get("is_completed")))
        solved_set = set(solved)
        unsolved = sorted(str(ch.get("id") or "") for ch in challenges
                          if ch.get("id") and str(ch.get("id")) not in solved_set)
        self._progress = {
            "total_challenges": len(challenges),
            "solved_ids": solved,
            "unsolved_ids": unsolved,
            "updated_at": time.time(),
        }
        if self.progress_path is not None:
            try:
                self.progress_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.progress_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(
                    self._progress, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                tmp.replace(self.progress_path)
            except OSError as exc:
                self._record("progress_persist_failed", error=str(exc)[:200])

    def progress(self) -> dict[str, Any]:
        return {**self._progress,
                "solved_ids": list(self._progress.get("solved_ids") or []),
                "unsolved_ids": list(self._progress.get("unsolved_ids") or [])}

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout, headers=dict(self.profile.headers),
                # 重定向手动跟随（_request）：httpx 自动跟随时只会剥离
                # Authorization，profile.headers 里的平台凭据头（Token/
                # X-Api-Key 等自定义头）会被原样带往任意目标站
                follow_redirects=False)
        return self._client

    async def aclose(self) -> None:
        self.closed = True
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None

    def _render_url(self, op: PlatformOp, extra: dict[str, str] | None = None) -> str:
        ctx = extra or {}
        if op.url:
            return str(_render(op.url, ctx))
        path = str(_render(op.path, ctx))
        if path.startswith(("http://", "https://")):
            return path
        base = self.profile.base_url
        if not base:
            return path
        return f"{base}/{path.lstrip('/')}" if path else base

    async def _request(self, op: PlatformOp,
                       ctx: dict[str, str] | None = None) -> _Resp:
        ctx = ctx or {}
        url = self._render_url(op, ctx)
        query = _render(op.query, ctx) if op.query else None
        body = _render(op.body, ctx) if op.body else None
        client = self._ensure_client()
        method = op.http_method()
        try:
            resp = await client.request(method, url,
                                        params=query or None,
                                        json=body if body else None)
        except Exception as exc:  # noqa: BLE001 —— 传输失败交调用方定性
            return _Resp(False, 0, None, "",
                         f"{type(exc).__name__}: {str(exc)[:200]}")
        # 重定向手动跟随（httpx 自动跟随会把平台凭据头带往任意目标站，
        # 见 _ensure_client）：只跟同主机，且不允许 https→http 降级
        hops = 0
        while resp.is_redirect and hops < 3:
            location = str(resp.headers.get("location") or "").strip()
            if not location:
                break
            target = resp.url.join(location)
            if target.host != resp.url.host or (
                    resp.url.scheme == "https" and target.scheme == "http"):
                return _Resp(False, 0, None, "", (
                    "平台接口重定向到其他站点，已阻止凭据转发: "
                    f"{str(target)[:200]}"))
            hops += 1
            if resp.status_code in (301, 302, 303):
                method, query, body = "GET", None, None
            try:
                resp = await client.request(method, target,
                                            json=body if body else None)
            except Exception as exc:  # noqa: BLE001
                return _Resp(False, 0, None, "",
                             f"{type(exc).__name__}: {str(exc)[:200]}")
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001 —— 非 JSON 应答保留原文
            data = None
        return _Resp(True, int(resp.status_code), data, resp.text or "")

    # -------------------------------------------------- 预检与列题

    async def precheck(self) -> dict[str, Any]:
        """平台可达性预检（可选接口）：结果只作事实，不硬中断任务。"""
        op = self.profile.ops.get("precheck")
        if op is None:
            return {}
        resp = await self._request(op)
        fact = {"op": "precheck", "ok": bool(resp.ok and 200 <= resp.status < 300),
                "status": resp.status,
                "detail": (resp.text or resp.error)[:200]}
        self._record("precheck", **fact)
        return fact

    def owns(self, cid: str) -> bool:
        """该题是否由本适配器供给实例：无列题接口时视为全权持有。"""
        if not self.profile.ops.get("list"):
            return True
        return cid in self._listed

    def has_completion_signal(self) -> bool:
        return "is_completed" in self.profile.challenge_fields

    def is_completed(self, cid: str) -> bool:
        return bool((self._listed.get(cid) or {}).get("is_completed"))

    async def refresh(self, *, force: bool = False) -> list[dict[str, Any]]:
        """刷新题目清单（节流）；失败保留上次快照并落事实。"""
        op = self.profile.ops.get("list")
        if op is None:
            return list(self._listed.values())
        now = time.monotonic()
        if not force and now - self._listed_at < self._REFRESH_MIN_INTERVAL:
            return list(self._listed.values())
        self._listed_at = now
        resp = await self._request(op)
        if not resp.ok or not (200 <= resp.status < 300):
            self._record("list_error", status=resp.status,
                         detail=(resp.error or resp.text)[:200])
            return list(self._listed.values())
        items = self._extract_items(resp.data, op)
        fresh: dict[str, dict[str, Any]] = {}
        for raw in items:
            if not isinstance(raw, dict):
                continue
            mapped = self._map_challenge(raw)
            if mapped is not None:
                fresh[mapped["id"]] = mapped
        self._listed = fresh
        self._update_progress(list(fresh.values()))
        self._record("list_refreshed", count=len(fresh),
                     total_challenges=self._progress["total_challenges"],
                     solved=len(self._progress["solved_ids"]))
        return list(fresh.values())

    async def list_challenges(self) -> list[dict[str, Any]]:
        """列题（强制刷新，装配期一次）。"""
        if self.profile.ops.get("list") is None:
            return []
        return await self.refresh(force=True)

    def _extract_items(self, data: Any, op: PlatformOp) -> list[Any]:
        if op.items_field:
            found = _dig(data, op.items_field)
            return found if isinstance(found, list) else []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    return value
            for value in data.values():
                if isinstance(value, dict):
                    inner = self._extract_items(value, op)
                    if inner:
                        return inner
        return []

    def _map_challenge(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        mapped: dict[str, Any] = {}
        for inner, path in self.profile.challenge_fields.items():
            value = _dig(raw, path)
            if value is not None:
                mapped[inner] = value
        cid = _as_str(mapped.get("id"))
        if not cid:
            return None
        mapped["id"] = cid
        mapped.setdefault("title", "")
        if "url" not in mapped:
            url = _maybe_url(_as_str(mapped.get("container_addr")))
            if url:
                mapped["url"] = url
        mapped["platform"] = self.profile.name
        return mapped

    # -------------------------------------------------- 实例生命周期

    def _max_active(self) -> int:
        try:
            return max(0, int(self.profile.limits.get("max_active", 0) or 0))
        except _SIZE_ERRORS:  # noqa: BLE001 —— 档案里给了非法值就当不限
            return 0

    async def ensure_instance(self, challenge: dict[str, Any]) -> dict[str, Any] | None:
        """按题幂等供给容器：已有实例直回；名额满 / 平台拒绝回 None（下轮补位）。"""
        cid = _as_str(challenge.get("id"))
        op = self.profile.ops.get("start")
        if not cid or op is None:
            return challenge
        if cid in self._instances:
            addr = self._instances[cid]
            if addr and not challenge.get("url"):
                return {**challenge, "container_addr": addr, "url": _maybe_url(addr)}
            if addr or challenge.get("url"):
                return challenge
            # 已登记但地址空缺且题面无入口（平台异步供给/字段不符）：不能
            # 当成就绪回给调用方——该题将永远拿不到入口，还白占一个活跃
            # 名额；按退避重试供给（本题名额已占，不再走名额检查）
        else:
            limit = self._max_active()
            if limit and len(self._instances) >= limit:
                self._record("instance_wait", challenge=cid,
                             active=len(self._instances), limit=limit)
                return None
        now = time.monotonic()
        if now < self._start_blocked_until:
            return None
        resp = await self._request(op, {"challenge": cid})
        if not resp.ok:
            self._start_blocked_until = now + self._START_RETRY_BACKOFF
            self._record("instance_error", challenge=cid, error=resp.error)
            return None
        if not (200 <= resp.status < 300):
            self._start_blocked_until = now + self._START_RETRY_BACKOFF
            self._record("instance_error", challenge=cid, status=resp.status,
                         detail=resp.text[:200])
            return None
        addr = _as_str(_dig(resp.data, op.addr_field or "container_addr"))
        self._instances[cid] = addr
        if not addr and not challenge.get("url"):
            # 2xx 但没给地址：按未就绪处理（记录事实 + 退避再试），下轮补位
            self._start_blocked_until = now + self._START_RETRY_BACKOFF
            self._record("instance_addr_missing", challenge=cid,
                         status=resp.status, detail=resp.text[:160])
            return None
        self._record("instance_started", challenge=cid, addr=addr, status=resp.status)
        if addr:
            return {**challenge, "container_addr": addr, "url": _maybe_url(addr)}
        return {**challenge}

    async def close_instance(self, cid: str, *, reason: str = "") -> bool:
        op = self.profile.ops.get("close")
        if op is None or not cid:
            self._instances.pop(cid, None)   # 平台无关闭能力：本地销账即可
            return False
        resp = await self._request(op, {"challenge": cid})
        ok = bool(resp.ok and 200 <= resp.status < 300)
        if ok:
            # 成功才销账：请求失败记录留档，close_all 收尾还能重试；
            # 先销账会让失败被永久遗忘，平台容器继续占用启动名额
            self._instances.pop(cid, None)
        self._record("instance_closed" if ok else "instance_close_failed",
                     challenge=cid, status=resp.status, reason=reason[:80],
                     detail="" if ok else (resp.error or resp.text)[:160])
        return ok

    async def restart_instance(self, challenge: dict[str, Any]) -> dict[str, Any] | None:
        """重启容器：先关后开（平台操作，属常规执行不受审批门约束）。"""
        cid = _as_str(challenge.get("id"))
        await self.close_instance(cid, reason="重启")
        await asyncio.sleep(0.5)
        return await self.ensure_instance(challenge)

    async def close_all(self, *, reason: str = "会话结束") -> int:
        """关闭全部活跃实例（会话收尾的兜底回收），返回关闭数。"""
        closed = 0
        for cid in list(self._instances):
            if await self.close_instance(cid, reason=reason):
                closed += 1
        return closed

    async def release_completed(
            self, accepted_ids: set[str] | None = None) -> list[str]:
        """回收已完成赛题的容器（释放活跃名额）。

        平台声明了 is_completed 映射时以平台完成态为准（多 Flag 题拿到
        一个 Flag 不等于完赛，不早退）；未声明时以「首个被平台判对的
        Flag」为完成信号。有完成态时顺带按轮刷新清单——回收决定与复盘
        收手门共用同一份新鲜完成态。返回本轮关闭的题 id。
        """
        if self.closed:
            return []
        done: set[str] = set()
        if self.has_completion_signal():
            await self.refresh()
            done = {cid for cid in list(self._instances) if self.is_completed(cid)}
        else:
            done = set(accepted_ids or set())
        released: list[str] = []
        for cid in list(self._instances):
            if cid in done and await self.close_instance(cid, reason="赛题完成"):
                released.append(cid)
        return released

    def all_completed(self) -> bool:
        """平台视野内是否全部赛题完成（无完成态信号或无清单 → False）。

        供复盘收手门用：拿到一个 Flag 不等于全部赛题达成，只有平台把
        清单内每一题都标成完成才允许走确定性收手快路。
        """
        if not self.has_completion_signal() or not self._listed:
            return False
        return all(bool(ch.get("is_completed")) for ch in self._listed.values())

    # -------------------------------------------------- Flag 提交

    async def _after_accepted(self, cid: str) -> None:
        """通关后立即归还容器名额，并通知主调度补位。"""
        if self.has_completion_signal() and (self.progress_path or self.continuous):
            await self.refresh(force=True)
        if self.continuous and self.is_completed(cid):
            await self.close_instance(cid, reason="提交确认通关，立即切题")
            self.completion_queue.put_nowait(cid)

    async def submit(self, flag_value: str, challenge_id: str) -> SubmitResult:
        """提交 Flag（FlagManager 的 SubmitFn 形态）。

        accepted 判定优先级：声明判定字段取布尔 → 重复提交业务码（该 Flag
        已计分，幂等视为正确）→ 其余明确 4xx 视为错误。传输失败 / 5xx /
        平台未给判定 → accepted=None（FlagManager 退避重试，不误杀候选）。
        """
        op = self.profile.ops.get("submit")
        if op is None:
            return SubmitResult(None, "平台档案未声明提交接口")
        resp = await self._request(op, {"challenge": challenge_id, "flag": flag_value})
        if not resp.ok:
            self._record("submit_error", challenge=challenge_id, error=resp.error)
            return SubmitResult(None, f"提交传输失败: {resp.error}")
        code = str((resp.data or {}).get("code") or "") \
            if isinstance(resp.data, dict) else ""
        lower = resp.text[:300].lower()
        if self._is_duplicate(op, code, resp.status, lower):
            self._record("submit_duplicate", challenge=challenge_id, status=resp.status)
            await self._after_accepted(challenge_id)
            return SubmitResult(True, f"平台回报重复提交（该 Flag 已计分）: {resp.text[:200]}")
        if 200 <= resp.status < 300:
            accepted: bool | None = None
            if op.accepted_field:
                value = _dig(resp.data, op.accepted_field)
                if value is not None:
                    accepted = bool(value)
            self._record("submitted", challenge=challenge_id, status=resp.status,
                         accepted=accepted, detail=resp.text[:160])
            # 提交确认即刷新平台完成态并落盘，多 Flag 题仍以整题通关为准。
            if accepted is True:
                await self._after_accepted(challenge_id)
            return SubmitResult(accepted, resp.text[:500])
        if resp.status >= 500:
            self._record("submit_error", challenge=challenge_id, status=resp.status,
                         detail=resp.text[:160])
            return SubmitResult(None, f"平台暂时不可用 HTTP {resp.status}: {resp.text[:200]}")
        self._record("submitted", challenge=challenge_id, status=resp.status,
                     accepted=False, detail=resp.text[:160])
        return SubmitResult(False, f"HTTP {resp.status}: {resp.text[:300]}")

    @staticmethod
    def _is_duplicate(op: PlatformOp, code: str, status: int, lower_text: str) -> bool:
        if op.duplicate_code and code == op.duplicate_code:
            return True
        # 未声明业务码时的兜底：只认平台语义明确的重复标记，不扩散猜测
        return status == 409 and (code == "duplicate" or "duplicate" in lower_text)

    # -------------------------------------------------- 摘要

    def summary(self) -> dict[str, Any]:
        progress = self.progress()
        return {
            "platform": self.profile.name, "base_url": self.profile.base_url,
            "listed": len(self._listed), "active_instances": len(self._instances),
            "limits": dict(self.profile.limits),
            "total_challenges": progress["total_challenges"],
            "solved_ids": progress["solved_ids"],
            "unsolved_ids": progress["unsolved_ids"],
        }


def parse_platform_profile(raw: dict[str, Any] | str) -> PlatformProfile:
    """档案入口：字典或 YAML/JSON 文本；非法即 ValueError（调用方降级）。"""
    if isinstance(raw, dict):
        return PlatformProfile.from_dict(raw)
    return PlatformProfile.from_yaml(str(raw))
