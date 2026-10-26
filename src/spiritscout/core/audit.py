"""JSONL 审计记录与工具调用摘要。

同一路径共享递增序号；工具参数附加展示摘要，读取时移除加密副本字段。"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def tool_preview(arguments: Any) -> str:
    """轨迹条短摘要：只提取工具目标与方法（完整参数在 arguments 原文里）。"""
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


def prepare_detail(value: Any) -> Any:
    """detail 原样保留，只为工具调用项附加轨迹条摘要（preview）。"""
    if isinstance(value, dict):
        if isinstance(value.get("name"), str) and isinstance(value.get("arguments"), dict):
            value = {**value, "preview": value.get("preview") or tool_preview(value["arguments"])}
        return {k: prepare_detail(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [prepare_detail(v) for v in value]
    return value


def sanitize_record(record: dict[str, Any]) -> dict[str, Any]:
    """读取口统一走这里：剥掉加密副本字段（SSE/下载不需要），不脱敏。

    脱敏时代落盘的旧记录读出来仍是当时写下的 REDACTED 形态——有损不可
    逆；新记录起原文落盘、原文转播。
    """
    detail = record.get("detail", {})
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
            # 模型入口参数另存加密副本（执行台账），供旧审计记录的参数查看
            # 兼容读取；新记录的 arguments 已原文落盘，SSE 直达。
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
            "detail": prepare_detail(detail),
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
            "detail": prepare_detail(detail),
        }
        self.records.append(record)
        return record

    def export(self, dest: str | Path) -> Path:  # pragma: no cover
        raise NotImplementedError

    def read_all(self) -> list[dict[str, Any]]:
        return list(self.records)
