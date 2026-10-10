"""黑板存储：SQLite 强一致 + 版本号乐观锁 + 写入权限分级 + 事件发布。

设计书 6.1 一致性机制：
- 全局唯一 ID + 版本号控制（写入校验版本，避免脏数据覆盖）；
- 写入统一经汇聚引擎校验通过后才发布事件（6.9 写入校验）；
- 事件携带「黑板层 + 数据 ID + 版本」，消费方幂等。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spiritscout.blackboard.layers import can_write
from spiritscout.bus.base import EventBus
from spiritscout.core.exceptions import PolicyViolation, VersionConflict
from spiritscout.core.models import AgentType, BlackboardLayer, Event

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bb_records (
    layer       TEXT NOT NULL,
    record_id   TEXT NOT NULL,
    version     INTEGER NOT NULL,
    dedup_key   TEXT NOT NULL DEFAULT '',
    data        TEXT NOT NULL,
    updated_by  TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (layer, record_id)
);
CREATE INDEX IF NOT EXISTS idx_bb_dedup ON bb_records(layer, dedup_key);
"""


class Blackboard:
    """结构化分层黑板（8 层）。线程安全；事件发布走环形总线。"""

    def __init__(self, db_path: str | Path, bus: EventBus | None = None):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        #：WAL + busy_timeout——Web 控制台/CLI status 等读进程与
        # 运行中写提交并发时不再瞬态 database is locked（读写不互堵，写等待 5s）
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()
        self._bus = bus

    # ------------------------------------------------------------ 基础读写

    def write(
        self,
        layer: BlackboardLayer,
        record_id: str,
        data: dict[str, Any],
        *,
        agent_type: AgentType = AgentType.MASTER,
        updated_by: str = "",
        dedup_key: str = "",
        expected_version: int | None = None,
        priority: int = 5,
    ) -> int:
        """写入记录并发布事件，返回新版本号。

        - 写权限分级：agent_type 无该层写权限 → PolicyViolation（4.8.3）；
        - 乐观锁：expected_version 与库中不一致 → VersionConflict（6.1）。
        """
        if not can_write(agent_type, layer):
            raise PolicyViolation(f"{agent_type.value} 无黑板层 {layer.value} 写入权限")
        now = datetime.now(UTC).isoformat()
        payload = json.dumps(data, ensure_ascii=False)
        with self._lock:
            row = self._conn.execute(
                "SELECT version FROM bb_records WHERE layer=? AND record_id=?",
                (layer.value, record_id),
            ).fetchone()
            current = row["version"] if row else 0
            if expected_version is not None and expected_version != current:
                raise VersionConflict(layer.value, record_id, expected_version, current)
            new_version = current + 1
            self._conn.execute(
                """INSERT INTO bb_records(layer, record_id, version, dedup_key, data, updated_by, updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(layer, record_id) DO UPDATE SET
                       version=excluded.version, dedup_key=excluded.dedup_key,
                       data=excluded.data, updated_by=excluded.updated_by,
                       updated_at=excluded.updated_at""",
                (layer.value, record_id, new_version, dedup_key, payload, updated_by, now),
            )
            self._conn.commit()
        self._publish(layer, record_id, new_version, data, updated_by, priority)
        return new_version

    def read(self, layer: BlackboardLayer, record_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM bb_records WHERE layer=? AND record_id=?",
                (layer.value, record_id),
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def read_versioned(self, layer: BlackboardLayer, record_id: str) -> tuple[dict[str, Any], int] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data, version FROM bb_records WHERE layer=? AND record_id=?",
                (layer.value, record_id),
            ).fetchone()
        return (json.loads(row["data"]), row["version"]) if row else None

    def query(
        self,
        layer: BlackboardLayer,
        *,
        dedup_key: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        sql = "SELECT record_id, version, data, updated_by, updated_at FROM bb_records WHERE layer=?"
        params: list[Any] = [layer.value]
        if dedup_key is not None:
            sql += " AND dedup_key=?"
            params.append(dedup_key)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "record_id": r["record_id"],
                "version": r["version"],
                "data": json.loads(r["data"]),
                "updated_by": r["updated_by"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    def find_by_dedup_key(self, layer: BlackboardLayer, dedup_key: str) -> dict[str, Any] | None:
        results = self.query(layer, dedup_key=dedup_key, limit=1)
        return results[0] if results else None

    def count(self, layer: BlackboardLayer) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM bb_records WHERE layer=?", (layer.value,)
            ).fetchone()
        return int(row["c"])

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ 事件

    def _publish(
        self,
        layer: BlackboardLayer,
        record_id: str,
        version: int,
        data: dict[str, Any],
        source: str,
        priority: int,
    ) -> None:
        if self._bus is None:
            return
        event = Event(
            topic=f"bb.{layer.value}",
            layer=layer,
            data_id=record_id,
            version=version,
            priority=priority,
            source=source,
            payload=data,
        )
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._bus.publish(event))
        except RuntimeError:
            # 无运行中的事件循环（同步上下文/测试）：静默跳过事件发布，存储仍生效
            pass
