"""进程内短期端点能力记忆；只记录明确拒绝且成功降级过的可选参数。"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable

from spiritscout.config.settings import ModelEntry


class EndpointCapabilities:
    def __init__(self, *, ttl_seconds: float = 600, capacity: int = 128,
                 clock: Callable[[], float] = time.monotonic, enabled: bool = True):
        self._ttl, self._capacity, self._clock = ttl_seconds, capacity, clock
        self._enabled = enabled
        self._entries: OrderedDict[tuple[str, str], float] = OrderedDict()

    @staticmethod
    def _key(entry: ModelEntry, anthropic: bool, option: str) -> tuple[str, str]:
        # 模型、协议和凭据切换都重新探测；内存键不保留地址或密钥原文。
        identity = json.dumps([entry.provider, entry.base_url, entry.model,
                               entry.api_format, anthropic, entry.api_key])
        return hashlib.sha256(identity.encode()).hexdigest(), option

    def unsupported(self, entry: ModelEntry, anthropic: bool, option: str) -> bool:
        if not self._enabled:
            return False
        key = self._key(entry, anthropic, option)
        expires = self._entries.pop(key, None)
        if expires is None or expires <= self._clock():
            return False
        self._entries[key] = expires
        return True

    def remember(self, entry: ModelEntry, anthropic: bool, option: str,
                 error: Exception) -> None:
        """调用者确认省略该字段后成功，才可记忆；歧义和临时故障不缓存。"""
        if not self._enabled:
            return
        if option not in {"response_format", "stream_options"}:
            return
        if getattr(error, "status_code", None) not in {400, 422}:
            return
        message = str(error).casefold()
        names = {option, "json_object"} if option == "response_format" else {option}
        if not any(name in message for name in names):
            return
        if not any(phrase in message for phrase in (
            "not supported", "not support", "unsupported", "unknown parameter",
            "unrecognized parameter", "unexpected keyword", "unexpected parameter",
        )):
            return
        key = self._key(entry, anthropic, option)
        self._entries.pop(key, None)
        self._entries[key] = self._clock() + self._ttl
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)
