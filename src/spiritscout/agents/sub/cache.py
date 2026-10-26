"""子 Agent 内各路线共享的缓存，生命周期与 Agent 一致。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class _Entry:
    value: Any
    expires_at: float | None
    hits: int = 0


class LocalSharedCache:
    """命名空间隔离的进程内缓存（路线间复用中间结果，避免重复探测）。"""

    def __init__(self, default_ttl_seconds: float | None = None):
        self._store: dict[str, dict[str, _Entry]] = {}
        self._default_ttl = default_ttl_seconds
        self._writes = 0

    def get(self, namespace: str, key: str) -> Any | None:
        entry = self._store.get(namespace, {}).get(key)
        if entry is None:
            return None
        if entry.expires_at is not None and time.monotonic() > entry.expires_at:
            del self._store[namespace][key]
            return None
        entry.hits += 1
        return entry.value

    def put(self, namespace: str, key: str, value: Any, ttl_seconds: float | None = None) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        expires = time.monotonic() + ttl if ttl else None
        self._store.setdefault(namespace, {})[key] = _Entry(value=value, expires_at=expires)
        self._writes += 1

    def has(self, namespace: str, key: str) -> bool:
        return self.get(namespace, key) is not None

    def namespace_items(self, namespace: str) -> dict[str, Any]:
        return {
            k: e.value
            for k, e in self._store.get(namespace, {}).items()
            if e.expires_at is None or time.monotonic() <= e.expires_at
        }

    def clear(self) -> None:
        self._store.clear()

    def stats(self) -> dict:
        total_entries = sum(len(ns) for ns in self._store.values())
        total_hits = sum(e.hits for ns in self._store.values() for e in ns.values())
        return {"entries": total_entries, "hits": total_hits, "writes": self._writes,
                "namespaces": list(self._store)}
