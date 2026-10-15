"""审计日志（设计书 4.7 监控与审计模块的日志基座；合规自查 G7/G8）。

- JSONL 追加写，单调序号 + UTC 时间戳，全流程可追溯；
- 自动脱敏：api_key / password / secret / token 等字段不落明文（G8）；
- 覆盖：工具调用、决策变更、漏洞核验流转、Flag/漏洞提交、合规阻断。
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

_SENSITIVE_KEYS = re.compile(
    r"(api_?key|secret|password|passwd|token|authorization|cookie|credential)", re.IGNORECASE
)


def _redact(value: Any) -> Any:
    """递归脱敏：敏感键值替换为 ***REDACTED***。"""
    if isinstance(value, dict):
        return {
            k: ("***REDACTED***" if _SENSITIVE_KEYS.search(str(k)) else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


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
            content = self._path.read_text(encoding="utf-8") if self._path.exists() else ""
        dest.write_text(content, encoding="utf-8")
        return dest

    def read_all(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        return [
            json.loads(line)
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
