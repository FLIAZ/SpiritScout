"""扫描器适配：沿用沙箱、审批和审计，完整 JSONL 落盘后逐条共享线索。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shlex
import stat
import uuid
from pathlib import Path
from typing import Any

from spiritscout.config.settings import ScannerConfig
from spiritscout.core.models import AgentType, BlackboardLayer
from spiritscout.tools.invocation import current_invocation
from spiritscout.tools.sandbox import sandbox_exec
from spiritscout.tools.sandbox_paths import open_directory
from spiritscout.tools.scan_evidence import save_nuclei_evidence
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolResult, ToolSpec

SPEC = ToolSpec(
    name="nuclei_scan", kind=ToolKind.BUILTIN, risk=RiskLevel.INTRUSIVE,
    description="使用已安装的 Nuclei 与本地模板收集待验证线索；完整结果保存在会话 artifacts/scans。",
    allowed_params={"targets", "template_ids", "tags", "action"},
    action_required=True, automatic_replay=False,
)


def _strings(value: Any, name: str, *, required: bool = False) -> list[str]:
    if not isinstance(value, list) or (required and not value):
        raise ValueError(f"{name} 必须是{'非空' if required else ''}字符串数组")
    if len(value) > 100 or any(not isinstance(v, str) or not v.strip()
                              or len(v) > 4096 or any(c in v for c in '\r\n\x00')
                              or v.startswith('-') for v in value):
        raise ValueError(f"{name} 包含无效值或超过单次参数容量")
    return value


class NucleiScanner:
    def __init__(self, config: ScannerConfig, blackboard):
        self.config = config
        self.blackboard = blackboard
        # 同一会话的扫描共享一个槽，不占主流程或专项工具的执行槽。
        self.slot = asyncio.Semaphore(1)

    @property
    def templates(self) -> str:
        return str(Path(self.config.templates_dir).absolute())

    async def preflight(self, **ctx) -> ToolResult:
        """仅在沙箱内检查部署，不自动安装工具或下载模板。"""
        command = (f"command -v {shlex.quote(self.config.binary)} && "
                   f"test -d {shlex.quote(self.templates)}")
        return await sandbox_exec(command, timeout=5, **ctx)

    async def run(self, targets, template_ids=None, tags=None, action="", **ctx) -> ToolResult:
        control = ctx.pop('_scan_control', None)
        if control is not None:
            return await control.run_tool(SPEC.name, lambda: self.run(targets, template_ids, tags, action, **ctx))
        try:
            targets = _strings(targets, "targets", required=True)
            template_ids = _strings(template_ids or [], "template_ids")
            tags = _strings(tags or [], "tags")
            if not template_ids and not tags:
                raise ValueError("必须明确选择 template_ids 或 tags，不默认执行全模板扫描")
        except ValueError as exc:
            return ToolResult(tool=SPEC.name, ok=False, error=str(exc), returncode=2)
        async with self.slot:
            return await self._run(targets, template_ids, tags, ctx)

    def _write(self, record_id: str, data: dict[str, Any]) -> None:
        self.blackboard.write(BlackboardLayer.RECON_CLUE, record_id, data,
                              agent_type=AgentType.SCANNER, updated_by="scanner")

    async def _run(self, targets, template_ids, tags, ctx) -> ToolResult:
        workspace = Path(ctx["workspace_root"]).absolute()
        scan_id = f"scan-{uuid.uuid4().hex}"
        relative = Path("artifacts") / "scans" / scan_id
        with open_directory(workspace, relative, create=True) as directory:
            output = workspace / relative / "results.jsonl"
            command = ["env", "DISABLE_NUCLEI_TEMPLATES_PUBLIC_DOWNLOAD=true",
                       "DISABLE_NUCLEI_TEMPLATES_GITHUB_DOWNLOAD=true",
                       "DISABLE_NUCLEI_TEMPLATES_GITLAB_DOWNLOAD=true",
                       "DISABLE_NUCLEI_TEMPLATES_AWS_DOWNLOAD=true",
                       "DISABLE_NUCLEI_TEMPLATES_AZURE_DOWNLOAD=true",
                       self.config.binary, "-duc", "-ni", "-jsonl", "-silent", "-nc",
                       "-t", self.templates, "-o", str(output),
                       "-c", str(self.config.concurrency)]
            rate = min(self.config.rate_limit, ctx["guard"].scope.max_requests_per_second)
            if rate <= 0:
                return ToolResult(tool=SPEC.name, ok=False, error="扫描请求速率必须大于零", returncode=2)
            if rate < 1:
                if rate * 60 < 1:
                    return ToolResult(tool=SPEC.name, ok=False,
                                      error="当前请求速率低于扫描器每分钟一次的配置能力", returncode=2)
                command.extend(["-rlm", str(math.floor(rate * 60))])
            else:
                command.extend(["-rl", str(math.floor(rate))])
            for target in targets:
                command.extend(["-u", target])
            if template_ids:
                command.extend(["-id", ",".join(template_ids)])
            if tags:
                command.extend(["-tags", ",".join(tags)])
            summary = {"clue_type": "scanner_run", "value": scan_id, "scanner": "nuclei",
                       "targets": targets, "template_ids": template_ids, "tags": tags,
                       "status": "running", "raw_output": str(output),
                       "verification_status": "unverified"}
            self._write(scan_id, summary)
            result = None
            try:
                # 固定 argv 经逐项引用转入现有 bwrap；无自由命令行参数透传。
                result = await sandbox_exec(shlex.join(command),
                    timeout=self.config.timeout_seconds, **ctx)
                summary.update(status="completed" if result.ok and result.returncode == 0 else "failed",
                               returncode=result.returncode, timed_out=result.timed_out,
                               error=result.error, stderr=result.stderr[:2000],
                               execution_log=result.parsed.get("output_file", ""))
            except asyncio.CancelledError:
                summary["status"] = "cancelled"
                raise
            except Exception as exc:
                summary.update(status="failed", error=str(exc)[:2000])
                raise
            finally:
                # 超时和停止也保留已产生的部分线索，不能标成完整扫描成功。
                summary.update(self._publish(directory, output, scan_id))
                if summary["parse_errors"] and summary["status"] == "completed":
                    summary.update(status="failed", error="扫描原始输出不完整或格式无效，已保存可读线索")
                self._write(scan_id, summary)
            result.tool = SPEC.name
            result.ok = summary["status"] == "completed" and not summary["parse_errors"]
            result.parsed = summary
            result.stdout = json.dumps(summary, ensure_ascii=False)
            return result

    def _publish(self, directory: int, output: Path, scan_id: str) -> dict[str, Any]:
        count = errors = 0
        preview = []
        try:
            fd = os.open("results.jsonl", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return {"findings": 0, "parse_errors": 0, "output_present": False, "preview": []}
        except OSError:
            return {"findings": 0, "parse_errors": 1, "output_present": False, "preview": []}
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return {"findings": 0, "parse_errors": 1, "output_present": False, "preview": []}
            with os.fdopen(fd, "rb", closefd=False) as source:
                line_no = 0
                while line := source.readline(16 * 1024 * 1024 + 1):
                    line_no += 1
                    if len(line) > 16 * 1024 * 1024:
                        while line and not line.endswith(b'\n'):
                            line = source.readline(1024 * 1024)
                        errors += 1
                        continue
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict) or not row.get("template-id"):
                            raise ValueError("缺少 template-id")
                        info = row.get("info") or {}
                        data = {"clue_type": "scanner_finding", "scanner": "nuclei",
                                "scan_id": scan_id, "template_id": str(row["template-id"])[:500],
                                "value": str(row.get("matched-at") or row.get("host") or "")[:4096],
                                "name": str(info.get("name", ""))[:500],
                                "severity": str(info.get("severity", "unknown"))[:30],
                                "matcher_name": str(row.get("matcher-name", ""))[:500],
                                "verification_status": "unverified", "confidence": 0.5,
                                "raw_output": str(output), "raw_line": line_no}
                    except (ValueError, TypeError, AttributeError):
                        errors += 1
                        continue
                    identifier = hashlib.sha256(f"{scan_id}:{line_no}".encode()).hexdigest()[:24]
                    finding_id = f"scan-finding-{identifier}"
                    invocation = current_invocation.get()
                    data.update(finding_id=finding_id,
                        invocation_id=invocation.invocation_id if invocation else "",
                        scan_evidence=save_nuclei_evidence(directory,
                            (Path('artifacts') / 'scans' / scan_id).as_posix(), line_no, row))
                    self._write(finding_id, data)
                    count += 1
                    if len(preview) < 10:
                        preview.append(data)
        finally:
            os.close(fd)
        return {"findings": count, "parse_errors": errors, "output_present": True, "preview": preview}
