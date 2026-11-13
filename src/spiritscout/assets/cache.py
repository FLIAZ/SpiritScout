"""外部资产情报的会话间短期缓存；只保存脱敏候选，不保存凭据或请求。"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from spiritscout.assets.providers.base import ProviderCandidate, ProviderResult


class ProviderCache:
    """按 provider 与结构化查询复用结果，失败和过期结果不复用。"""

    def __init__(self, path: str | Path, *, ttl_seconds: int, max_entries: int = 256) -> None:
        self.path = Path(path)
        self.ttl_seconds = max(0, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))

    def get(self, key: str) -> ProviderResult | None:
        if not self.ttl_seconds or not self.path.exists():
            return None
        try:
            with sqlite3.connect(self.path) as db:
                row = db.execute(
                    "SELECT body, created FROM provider_cache WHERE cache_key=?", (key,)
                ).fetchone()
                if not row or time.time() - float(row[1]) >= self.ttl_seconds:
                    if row:
                        db.execute("DELETE FROM provider_cache WHERE cache_key=?", (key,))
                    return None
            data = json.loads(row[0])
            candidates = [ProviderCandidate(**item) for item in data.get("candidates", [])]
            return ProviderResult(
                provider=str(data.get("provider") or ""), status="cached",
                candidates=candidates, returned=int(data.get("returned") or 0),
                truncated=bool(data.get("truncated")), cached=True,
            )
        except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
            return None

    def put(self, key: str, result: ProviderResult) -> None:
        if not self.ttl_seconds or result.status != "ok":
            return
        body = json.dumps({
            "provider": result.provider,
            "returned": result.returned,
            "truncated": result.truncated,
            "candidates": [{
                "value": item.value, "asset_type": item.asset_type,
                "source_ref": item.source_ref, "observation": item.observation,
            } for item in result.candidates],
        }, ensure_ascii=False, separators=(",", ":"))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path) as db:
                db.execute("CREATE TABLE IF NOT EXISTS provider_cache ("
                           "cache_key TEXT PRIMARY KEY, body TEXT NOT NULL, created REAL NOT NULL)")
                db.execute("INSERT OR REPLACE INTO provider_cache VALUES (?, ?, ?)",
                           (key, body, time.time()))
                db.execute("DELETE FROM provider_cache WHERE cache_key IN ("
                           "SELECT cache_key FROM provider_cache ORDER BY created DESC LIMIT -1 OFFSET ?)",
                           (self.max_entries,))
        except (OSError, sqlite3.Error):
            return
