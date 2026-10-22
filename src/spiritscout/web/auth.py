"""控制台管理员凭据与会话认证。

管理员账号落在工作区 SQLite（``admin.db``），密码只保存 scrypt 加盐哈希；
登录成功后签发 HMAC 签名 Cookie。环境变量（含 ``.env`` 导入）在启动时
同步进数据库——忘记用户名/密码时改 ``.env`` 重启即可恢复。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from spiritscout.core.exceptions import ConfigurationError

SESSION_COOKIE = "ss_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600

_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_MAX_N = 2 ** 18
_SALT_BYTES = 16
_HASH_BYTES = 32
MAX_USERNAME_LENGTH = 128
MAX_PASSWORD_LENGTH = 1024
_DEFAULT_USERNAME = "spiritscout"
_ENV_ADMIN_SYNC = "SPIRITSCOUT_ADMIN_SYNC"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    """scrypt 加盐哈希；调用方不要保存明文。"""
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
        p=_SCRYPT_P, dklen=_HASH_BYTES, maxmem=_SCRYPT_MAXMEM)
    return (f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}"
            f"${_b64(salt)}${_b64(digest)}")


def verify_password(password: str, stored: str) -> bool:
    """校验存储格式 ``scrypt$n$r$p$salt$hash``；格式非法/参数越界即失败。"""
    try:
        algo, n_text, r_text, p_text, salt_text, hash_text = stored.split("$")
        n, r, p = int(n_text), int(r_text), int(p_text)
        salt, expected = _unb64(salt_text), _unb64(hash_text)
    except (ValueError, TypeError):
        return False
    if algo != "scrypt" or not (1 <= n <= _MAX_N):
        return False
    if not (1 <= r <= 32 and 1 <= p <= 16):
        return False
    if not (8 <= len(salt) <= 64 and 16 <= len(expected) <= 64):
        return False
    try:
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
            dklen=len(expected), maxmem=_SCRYPT_MAXMEM)
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(digest, expected)


# 用户不存在时也跑一次同代价的 scrypt，响应时间不泄露账号是否存在。
_DUMMY_HASH = hash_password(secrets.token_hex(16))


def _validate_credentials(username: str, password: str) -> None:
    if not username or len(username) > MAX_USERNAME_LENGTH:
        raise ConfigurationError(
            f"管理员用户名必须为 1-{MAX_USERNAME_LENGTH} 个字符")
    if any(ord(ch) < 0x20 or ch == ":" for ch in username):
        raise ConfigurationError("管理员用户名不能包含冒号或控制字符")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ConfigurationError(
            f"管理员密码不能超过 {MAX_PASSWORD_LENGTH} 个字符")


def _chmod_quiet(path: Path, mode: int = 0o600) -> None:
    """WSL DrvFs 等文件系统可能不支持 chmod；失败不影响可用性。"""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


class AdminStore:
    """工作区管理员库。每个实例绑定自己的根目录，避免测试之间串库。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.db_path = self.root / "admin.db"
        self.key_path = self.root / "auth.key"
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS admins ("
            "username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL)")
        return conn

    def has_admins(self) -> bool:
        if not self.db_path.exists():
            return False
        conn = self._connect()
        try:
            row = conn.execute("SELECT 1 FROM admins LIMIT 1").fetchone()
        finally:
            conn.close()
        return row is not None

    def _hash_for(self, username: str) -> str:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT password_hash FROM admins WHERE username=?",
                (username,)).fetchone()
        finally:
            conn.close()
        return str(row[0]) if row else ""

    def verify(self, username: str, password: str) -> bool:
        stored = self._hash_for(username) if username else ""
        matched = verify_password(password, stored or _DUMMY_HASH)
        return matched and bool(stored)

    def upsert(self, username: str, password: str) -> None:
        now = time.time()
        token = hash_password(password)
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO admins"
                        "(username, password_hash, created_at, updated_at) "
                        "VALUES(?,?,?,?) ON CONFLICT(username) DO UPDATE SET "
                        "password_hash=excluded.password_hash, "
                        "updated_at=excluded.updated_at",
                        (username, token, now, now))
            finally:
                conn.close()
        _chmod_quiet(self.db_path)

    def password_fingerprint(self, username: str) -> str:
        stored = self._hash_for(username)
        if not stored:
            return ""
        return hashlib.sha256(stored.encode("utf-8")).hexdigest()[:16]

    def sync_from_env(self, password_env: str, username_env: str,
                      admin_password_env: str) -> str | None:
        """把环境变量凭据导入数据库；空密码绝不删除已有账号。

        显式 ``SPIRITSCOUT_ADMIN_PASSWORD`` 优先于旧版
        ``SPIRITSCOUT_SERVER_PASSWORD``；``SPIRITSCOUT_ADMIN_SYNC=0``
        可关闭启动同步（完全由数据库管理员维护时使用）。
        """
        if os.environ.get(_ENV_ADMIN_SYNC, "").strip() == "0":
            return None
        password = (os.environ.get(admin_password_env, "")
                    or os.environ.get(password_env, ""))
        if not password:
            return None
        username = (os.environ.get(username_env, "").strip()
                    or _DEFAULT_USERNAME)
        _validate_credentials(username, password)
        if self.verify(username, password):
            return username
        self.upsert(username, password)
        return username

    # ------------------------------------------------------------ 会话

    def _secret(self) -> bytes:
        if self.key_path.exists():
            try:
                key = bytes.fromhex(self.key_path.read_text("ascii").strip())
            except (ValueError, UnicodeDecodeError) as exc:
                raise ConfigurationError(
                    f"会话密钥文件损坏: {self.key_path}") from exc
            if len(key) < 32:
                raise ConfigurationError(
                    f"会话密钥文件损坏: {self.key_path}")
            return key
        self.root.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        try:
            fd = os.open(self.key_path,
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return self._secret()
        with os.fdopen(fd, "wb") as fh:
            fh.write(key.hex().encode("ascii"))
        return key

    def _sign(self, body: str) -> str:
        return _b64(hmac.new(self._secret(), body.encode("ascii"),
                             hashlib.sha256).digest())

    def issue_session(self, username: str) -> str:
        now = int(time.time())
        payload = {
            "u": username,
            "iat": now,
            "exp": now + SESSION_TTL_SECONDS,
            "pv": self.password_fingerprint(username),
            "n": secrets.token_urlsafe(12),
        }
        body = "v1." + _b64(json.dumps(
            payload, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8"))
        return f"{body}.{self._sign(body)}"

    def verify_session(self, token: str) -> str | None:
        parts = token.split(".") if token else []
        if len(parts) != 3 or parts[0] != "v1":
            return None
        body = f"{parts[0]}.{parts[1]}"
        if not hmac.compare_digest(parts[2], self._sign(body)):
            return None
        try:
            payload = json.loads(_unb64(parts[1]).decode("utf-8"))
            username = str(payload["u"])
            expires = int(payload["exp"])
            fingerprint = str(payload["pv"])
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            return None
        if not username or expires < int(time.time()):
            return None
        current = self.password_fingerprint(username)
        if not current or not hmac.compare_digest(fingerprint, current):
            return None
        return username


class LoginRateLimiter:
    """按来源 IP 限制登录失败次数；单进程内存态，重启清零。"""

    def __init__(self, limit: int = 5, window: float = 300.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> list[float]:
        hits = [t for t in self._failures.get(key, [])
                if now - t < self.window]
        self._failures[key] = hits
        return hits

    def retry_after(self, key: str) -> int | None:
        now = self._clock()
        with self._lock:
            hits = self._prune(key, now)
            if len(hits) < self.limit:
                return None
            return max(1, int(self.window - (now - hits[0])) + 1)

    def failure(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            hits = self._prune(key, now)
            hits.append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


def parse_basic_header(value: str) -> tuple[str, str] | None:
    parts = value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(parts[1], validate=True)
        username, password = decoded.decode("utf-8").split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return None
    return username, password
