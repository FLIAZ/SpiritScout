"""Flag 统一提交模块（设计书 4.4.6）。

- 格式识别：Flag 格式正则模板可配置（flag{...}/ctf{...}/SCTF{...}）；
- 自动提取：各专项子 Agent 产出的文本统一过格式校验与去重；
- 自动提交：可插拔提交函数（赛方 API/Mock），限速 + 退避（4.5.3 频率管控）；
- 反馈闭环：提交失败发布反馈事件供子 Agent 调整；成功发布 flag.submit 事件。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from spiritscout.core.audit import AuditLogger, NullAuditLogger
from spiritscout.core.ids import new_id
from spiritscout.core.models import Event, FlagCandidate

DEFAULT_FLAG_PATTERNS = [
    r"(?i)\b[a-z0-9_]{0,32}flag\{[^}\s]{1,128}\}",   # flag{...} 及带前缀变体
    r"(?i)\b[a-z0-9_]{0,32}ctf\{[^}\s]{1,128}\}",    # 赛事前缀变体（PKWCTF/SCTF…）
]
# 赛方「频率限制」类应答的关键词（触发退避）
_RATE_LIMIT_HINTS = ("频率", "太快", "rate limit", "too fast", "too many", "slow down")


@dataclass
class SubmitResult:
    """提交应答（accepted=None 表示平台未明确判定，如频率限制）。"""

    accepted: bool | None
    response: str = ""


# 提交函数签名：async fn(flag_value, challenge_id) -> SubmitResult
SubmitFn = Callable[[str, str], Awaitable[SubmitResult]]


def extract_flags(text: str, patterns: list[str] | None = None) -> list[str]:
    """从文本提取符合格式的 Flag 候选（去重保序）。"""
    out: list[str] = []
    for pattern in patterns or DEFAULT_FLAG_PATTERNS:
        for m in re.finditer(pattern, text):
            if m.group(0) not in out:
                out.append(m.group(0))
    return out


class FlagManager:
    """Flag 候选的汇总/校验/去重/提交/反馈中枢。"""

    def __init__(
        self,
        *,
        flag_patterns: list[str] | None = None,
        submit_fn: SubmitFn | None = None,
        audit: AuditLogger | None = None,
        bus=None,
        rate_limit_per_minute: int = 10,
        backoff_base_seconds: float = 1.0,
        max_attempts: int = 3,
        store_path: str | Path | None = None,
    ):
        self.patterns = flag_patterns or list(DEFAULT_FLAG_PATTERNS)
        self.submit_fn = submit_fn
        self.audit = audit or NullAuditLogger()
        self.bus = bus
        self.min_interval = 60.0 / max(rate_limit_per_minute, 1)
        self.backoff_base = backoff_base_seconds
        self.max_attempts = max_attempts
        self.store_path = Path(store_path) if store_path else None
        self._flags: dict[str, FlagCandidate] = {}   # value → candidate（去重核心）
        self._last_submit = 0.0
        self._lock = asyncio.Lock()
        if self.store_path and self.store_path.exists():
            self._load()

    # ------------------------------------------------------------ 提取

    def extract(self, text: str, *, challenge_id: str, found_by: str) -> list[FlagCandidate]:
        """从文本提取候选并注册（格式校验 + 全局去重）。"""
        new_candidates: list[FlagCandidate] = []
        for value in extract_flags(text, self.patterns):
            if value in self._flags:
                continue
            cand = FlagCandidate(
                flag_id=new_id("flag"), challenge_id=challenge_id,
                value=value, format_ok=True, found_by=found_by,
            )
            self._flags[value] = cand
            new_candidates.append(cand)
            self.audit.log("flag.extracted", "flag_manager", {
                "flag_id": cand.flag_id, "challenge_id": challenge_id,
                "found_by": found_by, "value_preview": value[:12] + "..."})
        if new_candidates:
            self._persist()
        return new_candidates

    # ------------------------------------------------------------ 提交

    async def submit(self, cand: FlagCandidate) -> FlagCandidate:
        """提交单个候选：限速 → 退避重试 → 结果回写 → 反馈事件。"""
        if cand.submitted and cand.accepted is not None:
            return cand  # 已终判，幂等跳过
        if self.submit_fn is None:
            cand.submit_response = "未配置提交接口（submit_fn=None）"
            return cand
        async with self._lock:
            for attempt in range(1, self.max_attempts + 1):
                await self._throttle()
                try:
                    result = await self.submit_fn(cand.value, cand.challenge_id)
                except Exception as exc:  # noqa: BLE001 —— 提交异常按未决处理
                    result = SubmitResult(None, f"提交异常: {exc}")
                cand.submitted = True
                cand.accepted = result.accepted
                cand.submit_response = result.response[:500]
                self.audit.log("flag.submit", "flag_manager", {
                    "flag_id": cand.flag_id, "challenge_id": cand.challenge_id,
                    "attempt": attempt, "accepted": result.accepted,
                    "response": cand.submit_response[:200]})
                if result.accepted is not None:
                    break  # 明确判定（对/错）即终态
                # 频率限制类应答 → 指数退避后重试（4.5.3 频率管控）
                wait = self.backoff_base * (2 ** (attempt - 1))
                await asyncio.sleep(wait)
        await self._publish_feedback(cand)
        self._persist()
        return cand

    async def submit_all(self, *, only_unjudged: bool = True) -> dict[str, int]:
        """批量提交全部候选，返回统计。"""
        stats = {"submitted": 0, "accepted": 0, "rejected": 0, "pending": 0}
        for cand in list(self._flags.values()):
            if only_unjudged and cand.submitted and cand.accepted is not None:
                continue
            await self.submit(cand)
            stats["submitted"] += 1
            key = ("accepted" if cand.accepted else
                   "rejected" if cand.accepted is False else "pending")
            stats[key] += 1
        return stats

    # ------------------------------------------------------------ 查询

    def get(self, value: str) -> FlagCandidate | None:
        return self._flags.get(value)

    def all(self) -> list[FlagCandidate]:
        return list(self._flags.values())

    def accepted_flags(self) -> list[FlagCandidate]:
        return [c for c in self._flags.values() if c.accepted]

    def stats(self) -> dict[str, int]:
        return {
            "extracted": len(self._flags),
            "submitted": sum(1 for c in self._flags.values() if c.submitted),
            "accepted": sum(1 for c in self._flags.values() if c.accepted is True),
            "rejected": sum(1 for c in self._flags.values() if c.accepted is False),
        }

    # ------------------------------------------------------------ 内部

    async def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_submit)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_submit = time.monotonic()

    async def _publish_feedback(self, cand: FlagCandidate) -> None:
        if self.bus is None:
            return
        # 成功：标记题目完成、释放资源；失败：反馈子 Agent 调整提取策略（4.4.6）
        topic = "flag.submit" if cand.accepted else "flag.feedback"
        await self.bus.publish(Event(
            topic=topic, data_id=cand.flag_id, source="flag_manager",
            priority=1 if cand.accepted else 5,
            payload={"challenge_id": cand.challenge_id, "accepted": cand.accepted,
                     "found_by": cand.found_by, "response": cand.submit_response[:200]},
        ))

    def _persist(self) -> None:
        if not self.store_path:
            return
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(json.dumps(
            [c.model_dump(mode="json") for c in self._flags.values()],
            ensure_ascii=False, indent=2), encoding="utf-8")

    def _load(self) -> None:
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
            for item in data:
                cand = FlagCandidate.model_validate(item)
                self._flags[cand.value] = cand
        except (json.JSONDecodeError, ValueError):  # 损坏的存档不阻断启动
            pass
