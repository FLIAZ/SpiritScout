"""模型密钥保管库：加密落盘的本地密钥库（SQLite + Fernet）。

成熟自托管系统的通行做法——密钥加密存储于应用数据库，主密钥独立保管：
- 密文：Fernet（cryptography 标准实现，AES-128-CBC + HMAC 完整性校验）；
- 主密钥：工作区 ``vault.key``（首次写入自动生成，权限 0600）；
  无人值守部署可用环境变量 ``SPIRITSCOUT_VAULT_KEY`` 注入（Fernet 格式）；
- 数据库：工作区 ``vault.db``（SQLite，secrets 表：alias 主键 + 密文 + 时间）。

安全边界：明文密钥只在本进程内存中短暂存在（解析后的 ModelEntry），
API 永不回传、审计不含值；``models.yaml`` 里只出现 ``${ENV_VAR}`` 占位符。
密钥库条目按 alias 覆盖环境变量占位符（库内直填优先）。
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from spiritscout.config.settings import ModelEntry
from spiritscout.core.exceptions import SpiritScoutError

_ENV_MASTER_KEY = "SPIRITSCOUT_VAULT_KEY"
_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

_lock = threading.Lock()
_root: Path | None = None
_fernet: Fernet | None = None


def configure(root: str | Path) -> None:
    """指定工作区根目录（控制台启动时按配置注入；默认 .spiritscout）。"""
    global _root, _fernet
    root = Path(root)
    if _root != root:
        _root = root
        _fernet = None


def _db_path() -> Path:
    return (_root or Path(".spiritscout")) / "vault.db"


def _key_path() -> Path:
    return (_root or Path(".spiritscout")) / "vault.key"


def _master_key() -> bytes:
    env = os.environ.get(_ENV_MASTER_KEY, "").strip()
    if env:
        return env.encode()
    path = _key_path()
    if path.exists():
        return path.read_bytes().strip()
    key = Fernet.generate_key()          # 首次写入自动生成
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


def _cipher() -> Fernet:
    global _fernet
    if _fernet is None:
        try:
            _fernet = Fernet(_master_key())
        except (ValueError, TypeError) as exc:
            raise SpiritScoutError(
                f"密钥库主密钥无效（{_ENV_MASTER_KEY} 或 vault.key）: {exc}") from exc
    return _fernet


def _connect() -> sqlite3.Connection:
    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, timeout=5)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS secrets ("
        "alias TEXT PRIMARY KEY, ciphertext BLOB NOT NULL, updated_at REAL NOT NULL)")
    return conn


# ------------------------------------------------------------ 读写

def set_key(alias: str, key: str) -> None:
    """写入/覆盖（空串 = 清除）。只落密文；明文不落任何文件。"""
    if not key:
        drop_key(alias)
        return
    token = _cipher().encrypt(key.encode("utf-8"))
    with _lock:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO secrets(alias, ciphertext, updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(alias) DO UPDATE SET ciphertext=excluded.ciphertext, "
                    "updated_at=excluded.updated_at",
                    (alias, token, time.time()))
        finally:
            conn.close()


def drop_key(alias: str) -> None:
    with _lock:
        conn = _connect()
        try:
            with conn:
                conn.execute("DELETE FROM secrets WHERE alias=?", (alias,))
        finally:
            conn.close()


def rename_key(old_alias: str, new_alias: str) -> None:
    if old_alias == new_alias:
        return
    with _lock:
        conn = _connect()
        try:
            with conn:
                conn.execute("UPDATE OR REPLACE secrets SET alias=? WHERE alias=?",
                             (new_alias, old_alias))
        finally:
            conn.close()


def has_key(alias: str) -> bool:
    if not _db_path().exists():
        return False
    conn = _connect()
    try:
        row = conn.execute("SELECT 1 FROM secrets WHERE alias=?", (alias,)).fetchone()
        return row is not None
    finally:
        conn.close()


def _load_all() -> dict[str, str]:
    """全部可解密条目（主密钥变更导致不可解密的条目按未配置处理）。"""
    if not _db_path().exists():
        return {}
    conn = _connect()
    try:
        rows = conn.execute("SELECT alias, ciphertext FROM secrets").fetchall()
    finally:
        conn.close()
    out: dict[str, str] = {}
    for alias, blob in rows:
        try:
            out[str(alias)] = _cipher().decrypt(bytes(blob)).decode("utf-8")
        except (InvalidToken, ValueError):
            continue
    return out


def apply_keys(models: list[ModelEntry]) -> list[ModelEntry]:
    """密钥库覆盖到加载后的注册表（库内密钥优先于环境变量占位符）。"""
    keys = _load_all()
    return [m.model_copy(update={"api_key": keys[m.alias]})
            if m.alias in keys else m for m in models]


def key_status(entry: dict[str, Any]) -> dict[str, Any]:
    """从原始条目提取 key 状态（不展开、不回传密钥本身）。

    key_source：vault（密钥库已配置）/ env（环境变量）/ none（未配置）。
    """
    api_key = str(entry.get("api_key") or "")
    m = _ENV_REF.match(api_key)
    env = m.group(1) if m else ""
    alias = str(entry.get("alias") or "")
    if has_key(alias):
        source = "vault"
    elif env and os.environ.get(env):
        source = "env"
    else:
        source = "none"
    return {
        "key_env": env,
        "key_source": source,
        "key_configured": source != "none",
        "key_is_placeholder": bool(m) or not api_key,
    }
