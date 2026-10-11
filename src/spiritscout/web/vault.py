"""模型密钥保管库：加密落盘的本地密钥库（SQLite + Fernet）。

成熟自托管系统的通行做法——密钥加密存储于应用数据库，主密钥独立保管：
- 密文：Fernet（cryptography 标准实现，AES-128-CBC + HMAC 完整性校验）；
- 主密钥：工作区 ``vault.key``（首次写入自动生成，权限 0600）；
  无人值守部署可用环境变量 ``SPIRITSCOUT_VAULT_KEY`` 注入（Fernet 格式）；
- 数据库：工作区 ``vault.db``（SQLite，secrets 表：供应商名主键 + 密文 + 时间）。

安全边界：明文密钥只在本进程内存中短暂存在（解析后的 ModelEntry），
API 永不回传、审计不含值；``models.yaml`` 不落密钥（占位符也不用）。
密钥库条目按供应商名注入注册表——同一供应商下的模型共用一条密钥记录。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from spiritscout.config.settings import ModelEntry
from spiritscout.core.exceptions import SpiritScoutError

_ENV_MASTER_KEY = "SPIRITSCOUT_VAULT_KEY"

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
        "name TEXT PRIMARY KEY, ciphertext BLOB NOT NULL, updated_at REAL NOT NULL)")
    # 早期版本以 alias / model 为主键列，就地改名（现按供应商名保管密钥）
    cols = {row[1] for row in conn.execute("PRAGMA table_info(secrets)").fetchall()}
    if "name" not in cols:
        legacy = "alias" if "alias" in cols else "model" if "model" in cols else ""
        if legacy:
            conn.execute(f"ALTER TABLE secrets RENAME COLUMN {legacy} TO name")
            conn.commit()
    return conn


# ------------------------------------------------------------ 读写

def set_key(name: str, key: str) -> None:
    """写入/覆盖（空串 = 清除）。只落密文；明文不落任何文件。"""
    if not key:
        drop_key(name)
        return
    token = _cipher().encrypt(key.encode("utf-8"))
    with _lock:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO secrets(name, ciphertext, updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(name) DO UPDATE SET ciphertext=excluded.ciphertext, "
                    "updated_at=excluded.updated_at",
                    (name, token, time.time()))
        finally:
            conn.close()


def drop_key(name: str) -> None:
    with _lock:
        conn = _connect()
        try:
            with conn:
                conn.execute("DELETE FROM secrets WHERE name=?", (name,))
        finally:
            conn.close()


def rename_key(old_name: str, new_name: str) -> None:
    """供应商改名时密钥跟着走。"""
    if old_name == new_name:
        return
    with _lock:
        conn = _connect()
        try:
            with conn:
                conn.execute("UPDATE OR REPLACE secrets SET name=? WHERE name=?",
                             (new_name, old_name))
        finally:
            conn.close()


def has_key(name: str) -> bool:
    if not _db_path().exists():
        return False
    conn = _connect()
    try:
        row = conn.execute("SELECT 1 FROM secrets WHERE name=?", (name,)).fetchone()
        return row is not None
    finally:
        conn.close()


def get_key(name: str) -> str:
    """单个条目的明文密钥（仅服务器内部使用：拉取模型列表等；API 绝不回传）。"""
    return _load_all().get(name, "")


def _load_all() -> dict[str, str]:
    """全部可解密条目（主密钥变更导致不可解密的条目按未配置处理）。"""
    if not _db_path().exists():
        return {}
    conn = _connect()
    try:
        rows = conn.execute("SELECT name, ciphertext FROM secrets").fetchall()
    finally:
        conn.close()
    out: dict[str, str] = {}
    for name, blob in rows:
        try:
            out[str(name)] = _cipher().decrypt(bytes(blob)).decode("utf-8")
        except (InvalidToken, ValueError):
            continue
    return out


def apply_keys(models: list[ModelEntry]) -> list[ModelEntry]:
    """密钥库注入加载后的注册表（密钥的唯一来源）。

    模型不单独存密钥——按所属供应商取（provider_ref）。
    """
    keys = _load_all()
    return [m.model_copy(update={"api_key": keys[m.provider_ref]})
            if m.provider_ref and m.provider_ref in keys else m for m in models]


def key_status(entry: dict[str, Any], name: str) -> dict[str, Any]:
    """供应商密钥状态（不回传密钥本身）。

    key_source：vault（密钥库已配置）/ none（未配置）。
    """
    configured = has_key(name)
    return {
        "key_source": "vault" if configured else "none",
        "key_configured": configured,
    }
