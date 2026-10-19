"""从执行台账只读查看调用参数；不修改审计，也不重放工具。

新审计记录的参数已原文落盘（前端直接显示，不走这里）；本模块服务
脱敏时代落盘的旧记录——按 digest 从加密台账解密读取，原文返回。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


def read_tool_parameters(workspace: Path, tool: str, digest: str) -> dict[str, Any]:
    """按工具名与参数摘要精确关联；相同摘要的历史调用拥有相同参数。"""
    unavailable = {"available": False, "reason": "此调用尚无可读取的参数副本"}
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("参数摘要格式无效")
    db, key = workspace / "bb.db", workspace / "execution.key"
    if not db.is_file() or not key.is_file():
        return unavailable
    try:
        key_bytes = key.read_bytes()
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"tool_invocations", "execution_owner"}.issubset(tables):
                return unavailable
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tool_invocations)")}
            if "params_cipher" not in columns:
                return unavailable
            owner = conn.execute("SELECT key_digest FROM execution_owner WHERE singleton=1").fetchone()
            if not owner or owner[0] != hashlib.sha256(key_bytes).hexdigest():
                return {"available": False, "reason": "会话密钥与执行台账不匹配"}
            row = conn.execute("SELECT params_cipher FROM tool_invocations WHERE tool_name=? "
                               "AND params_digest=? AND params_cipher IS NOT NULL ORDER BY created_at DESC LIMIT 1",
                               (tool, digest)).fetchone()
        cipher_text = row[0] if row else None
        if cipher_text is None and (workspace / "audit.jsonl").is_file():
            with (workspace / "audit.jsonl").open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict) or record.get("type") != "agent.tool_call":
                        continue
                    detail = record.get("detail")
                    if not isinstance(detail, dict) or not isinstance(detail.get("calls"), list):
                        continue
                    for call in detail["calls"]:
                        if not isinstance(call, dict):
                            continue
                        metadata = call.get("arguments")
                        if (call.get("name") == tool and isinstance(metadata, dict) and metadata.get("digest") == digest
                                and call.get("arguments_cipher")):
                            cipher_text = call["arguments_cipher"].encode()
        if cipher_text is None:
            return unavailable
        parameters = json.loads(Fernet(key_bytes).decrypt(cipher_text))
        actual = hashlib.sha256(json.dumps(parameters, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if actual != digest:
            return {"available": False, "reason": "参数副本校验失败"}
        return {"available": True, "parameters": parameters}
    except (OSError, ValueError, InvalidToken, sqlite3.Error):
        return {"available": False, "reason": "参数副本暂时无法读取"}
