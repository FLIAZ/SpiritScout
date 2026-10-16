"""执行台账：同一会话 SQLite 内原子记录状态与事件，敏感结果只存密文。

此层不自动重放历史调用；running/unknown 记录必须由恢复流程核实。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from spiritscout.blackboard.store import Blackboard
from spiritscout.core.exceptions import ConfigurationError
from spiritscout.tools.invocation import ExecutionScope
from spiritscout.tools.run_lock import ExecutionRunLock
from spiritscout.tools.spec import ToolResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_schema_migrations (
    version INTEGER PRIMARY KEY, applied_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_invocations (
    invocation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
    tool_name TEXT NOT NULL, params_digest TEXT NOT NULL,
    execution_status TEXT NOT NULL, effect_status TEXT NOT NULL DEFAULT 'none',
    replay_policy TEXT NOT NULL, result_cipher BLOB,
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invocations_run ON tool_invocations(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_invocations_unknown ON tool_invocations(updated_at)
WHERE execution_status='unknown' OR (effect_status='unknown' AND execution_status<>'running');
CREATE TABLE IF NOT EXISTS runtime_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    invocation_id TEXT NOT NULL, kind TEXT NOT NULL,
    payload TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approval_records (
    invocation_id TEXT PRIMARY KEY REFERENCES tool_invocations(invocation_id),
    action TEXT NOT NULL, allow INTEGER NOT NULL, decided_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS execution_runs (
    run_id TEXT PRIMARY KEY, status TEXT NOT NULL,
    started_at REAL NOT NULL, finished_at REAL
);
CREATE TABLE IF NOT EXISTS execution_owner (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), key_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_checkpoints (
    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL, scope TEXT NOT NULL, checkpoint_key TEXT NOT NULL,
    phase TEXT NOT NULL, schema_version INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL, state_cipher BLOB NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_key ON runtime_checkpoints(scope,checkpoint_key,checkpoint_id);
"""

_V2_COLUMNS = {"params_cipher": "BLOB", "task_id": "TEXT NOT NULL DEFAULT ''",
               "route_id": "TEXT NOT NULL DEFAULT ''", "agent_id": "TEXT NOT NULL DEFAULT ''",
               "identity_id": "TEXT NOT NULL DEFAULT ''"}


class ExecutionJournal:
    def __init__(self, blackboard: Blackboard, workspace: Path, run_id: str):
        self.blackboard = blackboard
        self.run_id = run_id
        self.workspace = workspace.resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        key_path = workspace / "execution.key"
        with blackboard.transaction() as conn:
            has_owner = conn.execute("SELECT 1 FROM sqlite_master WHERE name='execution_owner'").fetchone()
            owner = (conn.execute("SELECT key_digest FROM execution_owner WHERE singleton=1").fetchone()
                     if has_owner else None)
        if owner is not None and key_path.exists():
            if hashlib.sha256(key_path.read_bytes()).hexdigest() != owner[0]:
                raise ConfigurationError("执行数据库属于其他工作区密钥，不能共用或接管")
        elif owner is not None:
            raise ConfigurationError("执行记录密钥 execution.key 缺失，请从备份恢复；未生成替代密钥")
        if not key_path.exists():
            with blackboard.transaction() as conn:
                for table, fields in {"tool_invocations": ("result_cipher", "params_cipher"),
                                      "runtime_checkpoints": ("state_cipher",)}.items():
                    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                    for field in fields:
                        if field in columns and conn.execute(
                                f"SELECT 1 FROM {table} WHERE {field} IS NOT NULL LIMIT 1").fetchone():
                            raise ConfigurationError(
                                "执行记录密钥 execution.key 缺失，请从备份恢复；未生成替代密钥")
        # 原子发布密钥文件，避免并发初始化读到半截密钥。
        temporary = workspace / f".execution-key-{os.urandom(8).hex()}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(Fernet.generate_key())
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, key_path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        key = key_path.read_bytes()
        self._cipher = Fernet(key)
        self._key_digest = hashlib.sha256(key).hexdigest()
        if owner is None:
            # 旧版没有归属表，认领前验证已有密文确实能被当前密钥解开，
            # 不能让持另一份已有密钥的工作区在升级时错误认领旧库。
            with blackboard.transaction() as conn:
                for table, fields in {"tool_invocations": ("params_cipher", "result_cipher"),
                                      "runtime_checkpoints": ("state_cipher",)}.items():
                    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                    for field in fields:
                        if field not in columns:
                            continue
                        sample = conn.execute(
                            f"SELECT {field} FROM {table} WHERE {field} IS NOT NULL LIMIT 1").fetchone()
                        if sample is not None:
                            try:
                                self._cipher.decrypt(sample[0])
                            except InvalidToken:
                                raise ConfigurationError("工作区密钥不能解密既有执行数据库，未接管") from None
        with blackboard.transaction() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tool_invocations)")}
            migrated = conn.execute("SELECT 1 FROM sqlite_master "
                                    "WHERE name='execution_schema_migrations'").fetchone()
            version = (conn.execute("SELECT COALESCE(MAX(version),0) "
                                    "FROM execution_schema_migrations").fetchone()[0]
                       if migrated else 0)
        if version > 3:
            raise ConfigurationError("执行台账版本高于当前程序支持版本，请使用匹配的程序")
        if version and version < 3:
            blackboard.backup_before_upgrade(3)
        if columns and not set(_V2_COLUMNS).issubset(columns):
            blackboard.backup_before_upgrade(2)
        # executescript 自带提交；这里只在初始化时建表，不与业务事务混用。
        with blackboard.transaction() as conn:
            conn.executescript(_SCHEMA)
            conn.execute("INSERT OR IGNORE INTO execution_schema_migrations VALUES(1, ?)",
                         (time.time(),))
        with blackboard.transaction() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tool_invocations)")}
            for field, declaration in _V2_COLUMNS.items():
                if field not in columns:
                    conn.execute(f"ALTER TABLE tool_invocations ADD COLUMN {field} {declaration}")
            conn.execute("INSERT OR IGNORE INTO execution_schema_migrations VALUES(2, ?)",
                         (time.time(),))
            # 绑定密钥身份而非绝对工作区路径，整体移动工作区仍可恢复；
            # 不同密钥的另一个工作区不能接管同一数据库。
            conn.execute("INSERT OR IGNORE INTO execution_owner VALUES(1, ?)",
                         (self._key_digest,))
            owner = conn.execute("SELECT key_digest FROM execution_owner WHERE singleton=1").fetchone()
            if owner[0] != self._key_digest:
                raise ConfigurationError("执行数据库属于其他工作区密钥，不能共用或接管")
            conn.execute("INSERT OR IGNORE INTO execution_schema_migrations VALUES(3, ?)",
                         (time.time(),))

    def begin(self, invocation_id: str, tool: str, digest: str, replay_policy: str, *,
              params: dict[str, Any] | None = None, scope: ExecutionScope | None = None) -> None:
        now = time.time()
        scope = scope or ExecutionScope()
        encrypted = self._encrypt(params) if params is not None else None
        with self.blackboard.transaction() as conn:
            conn.execute(
                "INSERT INTO tool_invocations(invocation_id,run_id,tool_name,params_digest,"
                "execution_status,replay_policy,created_at,updated_at,params_cipher,task_id,"
                "route_id,agent_id,identity_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (invocation_id, self.run_id, tool, digest, "not_started", replay_policy, now, now,
                 encrypted, scope.task_id, scope.route_id, scope.agent_id, scope.identity_id))
            self._event(conn, invocation_id, "not_started", now)

    def transition(self, invocation_id: str, status: str, *, effect: str = "none",
                   result: ToolResult | None = None) -> None:
        now = time.time()
        encrypted = (self._cipher.encrypt(json.dumps(
            asdict(result), ensure_ascii=False, default=str).encode())
                     if result is not None else None)
        with self.blackboard.transaction() as conn:
            cursor = conn.execute(
                "UPDATE tool_invocations SET execution_status=?, effect_status=?,"
                "result_cipher=COALESCE(?,result_cipher), updated_at=? WHERE invocation_id=?",
                (status, effect, encrypted, now, invocation_id))
            if cursor.rowcount != 1:
                raise ValueError("调用记录不存在")
            self._event(conn, invocation_id, status, now)

    @staticmethod
    def _event(conn: Any, invocation_id: str, status: str, now: float) -> None:
        conn.execute("INSERT INTO runtime_events(invocation_id,kind,payload,created_at)"
                     " VALUES(?,?,?,?)", (invocation_id, "invocation." + status,
                                          json.dumps({"status": status}), now))

    def result(self, invocation_id: str) -> ToolResult | None:
        """供内部核实使用；API 只展示元数据，不能直接暴露工具原文。"""
        with self.blackboard.transaction() as conn:
            row = conn.execute("SELECT result_cipher FROM tool_invocations WHERE invocation_id=?",
                               (invocation_id,)).fetchone()
        if row is None or row[0] is None:
            return None
        return ToolResult(**json.loads(self._cipher.decrypt(row[0])))

    def approval(self, invocation_id: str, action: str, allow: bool) -> None:
        """裁决持久化后才继续执行；重启后不把历史批准转为新调用的许可。"""
        with self.blackboard.transaction() as conn:
            conn.execute("INSERT INTO approval_records VALUES(?,?,?,?)",
                         (invocation_id, action, int(allow), time.time()))

    def _encrypt(self, data: Any) -> bytes:
        return self._cipher.encrypt(json.dumps(data, ensure_ascii=False, allow_nan=False).encode())

    def parameters(self, invocation_id: str) -> dict[str, Any] | None:
        """内部核对用，不通过公开列表接口暴露参数。"""
        with self.blackboard.transaction() as conn:
            row = conn.execute("SELECT params_cipher FROM tool_invocations WHERE invocation_id=?",
                               (invocation_id,)).fetchone()
        return json.loads(self._cipher.decrypt(row[0])) if row and row[0] else None

    def activate_run(self, lock: ExecutionRunLock) -> None:
        """持有排他运行锁后才核对旧状态，不凭 PID/时间猜测执行者是否存活。"""
        if not lock.owned or lock.workspace != self.workspace:
            raise ConfigurationError("核对历史执行状态前必须持有同一工作区运行锁")
        lock.acquire_database(self.blackboard.database_path)
        now = time.time()
        with self.blackboard.transaction() as conn:
            conn.execute("UPDATE execution_runs SET status='interrupted',finished_at=? "
                         "WHERE status='running' AND run_id<>?", (now, self.run_id))
            rows = conn.execute("SELECT invocation_id,execution_status,effect_status FROM "
                                "tool_invocations WHERE run_id<>? AND execution_status IN "
                                "('running','awaiting_approval','not_started')", (self.run_id,)).fetchall()
            for row in rows:
                status = "unknown" if row["execution_status"] == "running" else "cancelled"
                conn.execute("UPDATE tool_invocations SET execution_status=?,updated_at=? "
                             "WHERE invocation_id=?", (status, now, row["invocation_id"]))
                self._event(conn, row["invocation_id"], status, now)
            conn.execute("INSERT INTO execution_runs VALUES(?,'running',?,NULL)",
                         (self.run_id, now))

    def finish_run(self, status: str) -> None:
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("无效的运行终态")
        with self.blackboard.transaction() as conn:
            conn.execute("UPDATE execution_runs SET status=?,finished_at=? "
                         "WHERE run_id=? AND status='running'", (status, time.time(), self.run_id))

    def checkpoint(self, scope: str, key: str, phase: str, state: dict[str, Any]) -> int:
        """检查点与其事件原子提交。只保存，不自动执行其中的工具或 DAG 节点。"""
        encrypted = self._encrypt(state)
        now = time.time()
        with self.blackboard.transaction() as conn:
            event = conn.execute("INSERT INTO runtime_events(invocation_id,kind,payload,created_at)"
                                 " VALUES('', 'checkpoint.saved',?,?)",
                                 (json.dumps({"scope": scope, "phase": phase}), now))
            result = conn.execute("INSERT INTO runtime_checkpoints(run_id,scope,checkpoint_key,"
                                  "phase,schema_version,last_event_id,state_cipher,created_at) "
                                  "VALUES(?,?,?,?,1,?,?,?)",
                                  (self.run_id, scope, key, phase, event.lastrowid, encrypted, now))
            return int(result.lastrowid)

    def latest_checkpoint(self, scope: str, key: str) -> dict[str, Any] | None:
        with self.blackboard.transaction() as conn:
            row = conn.execute("SELECT * FROM runtime_checkpoints WHERE scope=? AND "
                               "checkpoint_key=? ORDER BY checkpoint_id DESC LIMIT 1",
                               (scope, key)).fetchone()
        if row is None:
            return None
        if row["schema_version"] != 1:
            raise ConfigurationError("不支持的检查点版本，未恢复执行")
        result = dict(row)
        result.pop("state_cipher")
        result["state"] = json.loads(self._cipher.decrypt(row["state_cipher"]))
        return result

    def planning_context(self, view: dict[str, Any]) -> dict[str, Any]:
        """将上次 DAG 的事实交给模型重规划，保留当前用户意图与审批门的优先权。"""
        context: dict[str, Any] = {
            "mode": "history_replan", "unresolved_count": view["unresolved_count"],
            "unresolved": view["unresolved"][:8],
            "notice": "历史记录不代表动作未发生。效果未知时先核实或询问用户，不直接重复有副作用的动作。",
        }
        checkpoint = self.latest_checkpoint("master", "master")
        if checkpoint is not None:
            state = checkpoint["state"]
            if not isinstance(state, dict) or not isinstance(state.get("dag", {}), dict):
                raise ConfigurationError("主调度检查点结构无效，未恢复执行")
            nodes = state.get("dag", {})
            context["mode"] = "checkpoint_replan"
            context["checkpoint"] = {
                "checkpoint_id": checkpoint["checkpoint_id"], "run_id": checkpoint["run_id"],
                "phase": checkpoint["phase"], "target": state.get("target", ""),
                "round_no": state.get("round_no", 0),
                "nodes": [{"node_id": node.get("node_id"), "tactic": node.get("tactic"),
                           "status": node.get("status"), "title": str(node.get("title", ""))[:160]}
                          for node in list(nodes.values())[-12:] if isinstance(node, dict)],
            }
        return context

    def list_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.blackboard.transaction() as conn:
            rows = conn.execute(
                "SELECT invocation_id,run_id,tool_name,params_digest,execution_status,"
                "effect_status,replay_policy,created_at,updated_at,task_id,route_id,agent_id,"
                "identity_id FROM tool_invocations"
                " ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()
        return [dict(row) for row in rows]


def read_execution_page(db_path: Path, *, events: bool = False,
                        after: int = 0, limit: int = 100) -> dict[str, Any]:
    """只读分页：历史会话不会因查看而创建密钥、执行迁移或恢复动作。"""
    empty: dict[str, Any] = {"items": [], "next_cursor": after, "has_more": False}
    if not db_path.exists():
        return empty
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("BEGIN")
    limit = max(1, min(limit, 500))
    try:
        table = "runtime_events" if events else "tool_invocations"
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            return empty
        if events:
            rows = conn.execute("SELECT event_id,invocation_id,kind,payload,created_at"
                                " FROM runtime_events WHERE event_id>? ORDER BY event_id LIMIT ?",
                                (after, limit + 1)).fetchall()
        else:
            fields = {row[1] for row in conn.execute("PRAGMA table_info(tool_invocations)")}
            extra = ",".join(name if name in fields else f"'' AS {name}"
                             for name in ("task_id", "route_id", "agent_id", "identity_id"))
            rows = conn.execute("SELECT rowid AS cursor,invocation_id,run_id,tool_name,params_digest,"
                                "execution_status,effect_status,replay_policy,created_at,updated_at"
                                f",{extra} FROM tool_invocations WHERE rowid>? ORDER BY rowid LIMIT ?",
                                (after, limit + 1)).fetchall()
        items = [dict(row) for row in rows[:limit]]
        if events:
            for item in items:
                item["payload"] = json.loads(item["payload"])
        return {"items": items, "has_more": len(rows) > limit,
                "next_cursor": items[-1]["event_id" if events else "cursor"] if items else after}
    finally:
        conn.close()


def read_recovery_view(db_path: Path, *, include_inflight: bool = False) -> dict[str, Any]:
    """提供核对线索，不解密参数/对话，也不将检查点直接当作重放指令。"""
    view: dict[str, Any] = {"runs": [], "checkpoints": [], "unresolved": [],
                            "unresolved_count": 0, "mode": "history_replan"}
    if not db_path.exists():
        return view
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("BEGIN")
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "execution_runs" in tables:
            view["runs"] = [dict(row) for row in conn.execute(
                "SELECT run_id,status,started_at,finished_at FROM execution_runs "
                "ORDER BY started_at DESC LIMIT 10")]
        if "runtime_checkpoints" in tables:
            view["checkpoints"] = [dict(row) for row in conn.execute(
                "SELECT checkpoint_id,run_id,scope,checkpoint_key,phase,schema_version,"
                "last_event_id,created_at FROM runtime_checkpoints WHERE checkpoint_id IN "
                "(SELECT MAX(checkpoint_id) FROM runtime_checkpoints GROUP BY scope,checkpoint_key)"
                " ORDER BY checkpoint_id DESC LIMIT 50")]
            if conn.execute("SELECT 1 FROM runtime_checkpoints WHERE scope='master' LIMIT 1").fetchone():
                view["mode"] = "checkpoint_replan"
        if "tool_invocations" in tables:
            condition = ("execution_status='unknown' OR (effect_status='unknown' "
                         "AND execution_status<>'running')")
            if include_inflight:
                # 会话已停止/中断时，旧 running 只是最后一次落盘状态，并非存活证明。
                condition += " OR execution_status='running'"
            view["unresolved_count"] = conn.execute(
                f"SELECT count(*) FROM tool_invocations WHERE {condition}").fetchone()[0]
            view["unresolved"] = [dict(row) for row in conn.execute(
                "SELECT invocation_id,run_id,tool_name,execution_status,effect_status,updated_at "
                f"FROM tool_invocations WHERE {condition} ORDER BY updated_at DESC LIMIT 50")]
        return view
    finally:
        conn.close()
