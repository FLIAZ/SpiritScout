"""审计日志（设计书 4.7 监控与审计模块的日志基座；合规自查 G7/G8）。

- JSONL 追加写，单调序号 + UTC 时间戳，全流程可追溯；
- 自动脱敏：api_key / password / secret / token 等字段不落明文（G8）；
- 覆盖：工具调用、决策变更、漏洞核验流转、Flag/漏洞提交、合规阻断。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SENSITIVE_KEYS = re.compile(
    r"(api_?key|secret|password|passwd|token|authorization|cookie|credential)", re.IGNORECASE
)
_RAW_KEYS = {"arguments", "params", "code", "command", "body", "request_body", "headers"}
_COUNTER_KEYS = {"tokens", "used_tokens", "prompt_tokens", "completion_tokens", "total_tokens",
                 "input_tokens", "output_tokens", "reasoning_tokens", "budget_tokens",
                 "cached_input_tokens", "cache_write_tokens"}
_AUTH_VALUE = re.compile(r"(?i)(\b(?:Bearer|Basic)\s+)[^\s\"'<>]+")
_NAMED_VALUE = re.compile(
    r"(?i)((?:api[_-]?key|password|passwd|secret|token|authorization|cookie|credential)"
    r"[\w-]*[\"']?\s*[:=]\s*[\"']?)[^\s\"'&;,}]+")
_URL_USERINFO = re.compile(r"(?i)(https?://)[^/\s@]+@")


def tool_preview(arguments: Any) -> str:
    """只提取工具目标和方法；不展示脚本、请求正文、凭据或查询参数值。"""
    if not isinstance(arguments, dict):
        return ""
    raw = arguments.get("url") or arguments.get("target")
    parts: list[str] = []
    method = arguments.get("method")
    if isinstance(method, str) and method.upper() in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
        parts.append(method.upper())
    if isinstance(raw, str):
        try:
            url = urlsplit(raw)
            if url.scheme in {"http", "https"} and url.hostname:
                host = url.hostname
                host = f"[{host}]" if ":" in host else host
                if url.port:
                    host += f":{url.port}"
                # 路径中的长随机段不进入展示摘要，查询仅保留参数名。
                path = "/".join("…" if len(segment) > 32 else segment for segment in url.path.split("/"))
                query = urlencode([(name[:80], "…") for name, _ in parse_qsl(url.query)][:12])
                parts.append(urlunsplit((url.scheme, host, path, query, "")))
        except ValueError:
            pass
    return " ".join(parts)[:300]


def _opaque(value: Any) -> dict[str, Any]:
    """审计不复制原始脚本/请求；完整工具参数由执行台账加密保管。"""
    if (isinstance(value, dict) and set(value) == {"_redacted", "digest"}
            and value["_redacted"] is True and isinstance(value["digest"], str)
            and re.fullmatch(r"[0-9a-f]{64}", value["digest"])):
        return value
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    return {"_redacted": True, "digest": hashlib.sha256(encoded).hexdigest()}


def _redact(value: Any) -> Any:
    """递归脱敏：敏感键值替换为 ***REDACTED***。"""
    if isinstance(value, dict):
        if isinstance(value.get("name"), str) and isinstance(value.get("arguments"), dict):
            value = {**value, "preview": value.get("preview") or tool_preview(value["arguments"])}
        return {
            k: (v if str(k) in _COUNTER_KEYS and (v is None or type(v) is int and v >= 0)
                else "***REDACTED***" if _SENSITIVE_KEYS.search(str(k))
                else _opaque(v) if str(k).lower() in _RAW_KEYS else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        value = _URL_USERINFO.sub(r"\1***REDACTED***@", value)
        value = _AUTH_VALUE.sub(r"\1***REDACTED***", value)
        return _NAMED_VALUE.sub(r"\1***REDACTED***", value)
    return value


def sanitize_record(record: dict[str, Any]) -> dict[str, Any]:
    """读取旧日志也走同一脱敏口，不将历史明文送入 SSE/下载。"""
    detail = _redact(record.get("detail", {}))
    if isinstance(detail, dict) and isinstance(detail.get("calls"), list):
        detail = {**detail, "calls": [
            {k: v for k, v in call.items() if k != "arguments_cipher"}
            if isinstance(call, dict) else call for call in detail["calls"]]}
    return {**record, "detail": detail}


class AuditLogger:
    """线程安全的 JSONL 审计记录器。

    序号按文件路径全进程共享：同一审计文件可以有多个写入实例（执行链路
    常驻记录器、Web 层为用户消息/审批等事件临时创建的记录器），各自从 0
    计数会让同一文件里的 seq 撞车回退，下游按 seq 去重时会吞事件。
    """

    _file_seqs: ClassVar[dict[str, int]] = {}
    _file_seqs_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._key = str(self._path)
        with AuditLogger._file_seqs_lock:
            if self._key not in AuditLogger._file_seqs:
                # 续接磁盘已有行数（重启/后到的写入实例不从 0 重排）
                n = 0
                if self._path.exists():
                    with self._path.open("r", encoding="utf-8") as fh:
                        for _ in fh:
                            n += 1
                AuditLogger._file_seqs[self._key] = n

    def log(self, event_type: str, actor: str, detail: dict[str, Any]) -> dict[str, Any]:
        """写入一条审计记录。event_type 约定：

        tool.call / tool.result / decision.change / vuln.transition /
        flag.submit / scope.violation / policy.violation / model.call /
        budget.exceeded / human.approval / system.lifecycle
        """
        if event_type == "agent.tool_call" and isinstance(detail.get("calls"), list):
            # 模型入口参数可能与执行器转换后的参数不同，额外保存加密副本供会话查看。
            # 日志/SSE 仍不暴露正文，展示接口按当前会话密钥解密并脱敏。
            key = self._path.parent / "execution.key"
            if key.is_file():
                from cryptography.fernet import Fernet

                cipher = Fernet(key.read_bytes())
                calls = []
                for call in detail["calls"]:
                    if isinstance(call, dict) and isinstance(call.get("arguments"), dict):
                        payload = json.dumps(call["arguments"], ensure_ascii=False).encode()
                        call = {**call, "arguments_cipher": cipher.encrypt(payload).decode()}
                    calls.append(call)
                detail = {**detail, "calls": calls}
        with AuditLogger._file_seqs_lock:
            AuditLogger._file_seqs[self._key] += 1
            seq = AuditLogger._file_seqs[self._key]
        record = {
            "seq": seq,
            "ts": datetime.now(UTC).isoformat(),
            "type": event_type,
            "actor": actor,
            "detail": _redact(detail),
        }
        with self._lock, self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def export(self, dest: str | Path) -> Path:
        """一键导出审计日志（4.7 审计报告导出的原始数据）。"""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            content = "".join(json.dumps(record, ensure_ascii=False) + "\n"
                              for record in self.read_all())
        dest.write_text(content, encoding="utf-8")
        return dest

    def read_all(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        return [
            sanitize_record(json.loads(line))
            for line in self._path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class NullAuditLogger(AuditLogger):
    """不落盘的空实现（单元测试用）。"""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self._seq = 0

    def log(self, event_type: str, actor: str, detail: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        record = {
            "seq": self._seq,
            "ts": datetime.now(UTC).isoformat(),
            "type": event_type,
            "actor": actor,
            "detail": _redact(detail),
        }
        self.records.append(record)
        return record

    def export(self, dest: str | Path) -> Path:  # pragma: no cover
        raise NotImplementedError

    def read_all(self) -> list[dict[str, Any]]:
        return list(self.records)
