"""本机密钥库：SQLite 保存 Fernet 密文。

主密钥从 SPIRITSCOUT_VAULT_KEY 读取，或首次写入 vault.key（权限 0600）。
供应商名是记录主键；API 和审计不包含明文密钥。"""

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
    """供应商改名时密钥跟着走。

    先移除同名冲突再改名：``UPDATE OR REPLACE`` 的 REPLACE 语义会先
    删掉撞名行——那正是调用方刚写入的新密钥，绝不能被旧密钥覆盖。
    """
    if old_name == new_name:
        return
    with _lock:
        conn = _connect()
        try:
            with conn:
                conn.execute("DELETE FROM secrets WHERE name=?", (new_name,))
                conn.execute("UPDATE secrets SET name=? WHERE name=?",
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
