"""工具执行器：超时控制、参数校验、输出截断、重试与降级、回滚保护。

回滚保护（验收标准）：执行失败或超时后，将目标状态恢复到执行前快照。
对只读探测类工具回滚为空操作；对产生本地副作用的工具（写文件/会话），
执行前记录快照、失败后清理（如删除临时文件、关闭残留进程）。
"""

from __future__ import annotations

import abc
import asyncio
import shlex
import time
import uuid
from pathlib import Path
from typing import Any

from spiritscout.core.audit import AuditLogger, NullAuditLogger
from spiritscout.core.exceptions import ToolExecutionError
from spiritscout.tools.spec import ToolKind, ToolResult, ToolSpec


class ToolExecutor(abc.ABC):
    def __init__(self, max_output_chars: int = 20000, audit: AuditLogger | None = None):
        self.max_output_chars = max_output_chars
        self.audit = audit or NullAuditLogger()

    @abc.abstractmethod
    async def run(self, spec: ToolSpec, params: dict[str, Any]) -> ToolResult: ...

    def _truncate(self, text: str) -> str:
        """输出窗口截断（HackSynth 3.2.4-9：防超长工具输出污染上下文）。"""
        if len(text) <= self.max_output_chars:
            return text
        head = self.max_output_chars * 3 // 4
        tail = self.max_output_chars // 4
        return text[:head] + f"\n...[截断 {len(text) - head - tail} 字符]...\n" + text[-tail:]

    def _validate_params(self, spec: ToolSpec, params: dict[str, Any]) -> None:
        if spec.allowed_params:
            unknown = set(params) - spec.allowed_params
            if unknown:
                raise ToolExecutionError(f"工具 {spec.name} 非法参数: {unknown}")

    @staticmethod
    def _quote(value: Any) -> str:
        """参数注入防护：所有槽位值强制 shell 转义。"""
        return shlex.quote(str(value))


class _RollbackStack:
    """执行副作用快照与回滚（文件清理/进程终止）。"""

    def __init__(self) -> None:
        self._created_files: list[Path] = []
        self._spawned: list[asyncio.subprocess.Process] = []

    def track_file(self, path: Path) -> None:
        self._created_files.append(path)

    def track_process(self, proc: asyncio.subprocess.Process) -> None:
        self._spawned.append(proc)

    async def rollback(self) -> bool:
        did = False
        for proc in self._spawned:
            if proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                    did = True
                except ProcessLookupError:
                    pass
        for path in self._created_files:
            try:
                path.unlink(missing_ok=True)
                did = True
            except OSError:
                pass
        return did


class LocalExecutor(ToolExecutor):
    """本地 subprocess 执行（默认后端）。"""

    async def run(self, spec: ToolSpec, params: dict[str, Any]) -> ToolResult:
        self._validate_params(spec, params)
        started = time.perf_counter()
        rollback = _RollbackStack()
        # subprocess_exec 走 argv 数组、不经 shell：参数按字面值传递，天然防注入
        cmd = [part.format(**{k: str(v) for k, v in params.items()})
               for part in spec.command_template]
        self.audit.log("tool.call", "executor",
                       {"tool": spec.name, "kind": spec.kind.value, "params": params})
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            rollback.track_process(proc)
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=spec.timeout_seconds
                )
                timed_out = False
            except TimeoutError:
                timed_out = True
                proc.kill()
                stdout_b, stderr_b = await proc.communicate()
            duration = (time.perf_counter() - started) * 1000
            stdout = self._truncate(stdout_b.decode(errors="replace"))
            stderr = self._truncate(stderr_b.decode(errors="replace"))
            ok = not timed_out and (proc.returncode == 0)
            rolled = False
            if not ok:
                rolled = await rollback.rollback()
            result = ToolResult(
                tool=spec.name, ok=ok, returncode=proc.returncode or -1,
                stdout=stdout, stderr=stderr, duration_ms=duration,
                timed_out=timed_out, attempts=1, rolled_back=rolled,
                error="执行超时" if timed_out else ("" if ok else f"退出码 {proc.returncode}"),
            )
            if spec.parser:
                try:
                    result.parsed = spec.parser(stdout, stderr, result.returncode)
                except Exception as exc:  # noqa: BLE001 —— 解析失败不致命
                    result.parsed = {"parse_error": str(exc)}
            self.audit.log("tool.result", "executor", {
                "tool": spec.name, "ok": ok, "rc": result.returncode,
                "timed_out": timed_out, "duration_ms": round(duration, 1),
            })
            return result
        except FileNotFoundError as exc:
            return ToolResult(tool=spec.name, ok=False, returncode=127,
                              error=f"工具未安装: {spec.command_template[0]} ({exc})")


class DockerExecutor(ToolExecutor):
    """Docker 沙箱执行（子 Agent 隔离；9.3 EXP 沙箱验证， 加固）。

    加固基线（最小权限 + 资源限额 + 可回滚）：
    - ``--cap-drop ALL`` + ``no-new-privileges``：容器内进程无额外特权；
    - ``--read-only`` + 受限 tmpfs /tmp：根文件系统只读，落盘仅小容量 noexec 临时区；
    - ``--memory/--cpus/--pids-limit``：资源硬限额（防 fork 炸弹/内存耗尽拖垮宿主）；
    - ``--network none``：CTF 模式网络隔离（硬约束 C7）；
    - 命名容器 + 超时 ``docker kill``：docker CLI 被杀不残留运行中容器（回滚保护）。
    """

    def __init__(self, image: str = "kalilinux/kali-rolling:latest",
                 network_isolation: bool = False, *,
                 memory_mb: int = 512, cpus: float = 1.0, pids_limit: int = 256,
                 read_only: bool = True,
                 tmpfs: str = "/tmp:rw,noexec,nosuid,size=64m",
                 user: str = "", **kw):
        super().__init__(**kw)
        self.image = image
        self.network_isolation = network_isolation
        self.memory_mb = memory_mb
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.read_only = read_only
        self.tmpfs = tmpfs
        self.user = user

    def _docker_cmd(self, spec: ToolSpec, params: dict[str, Any],
                    container_name: str) -> list[str]:
        inner = " ".join(
            part.format(**{k: self._quote(v) for k, v in params.items()})
            for part in spec.command_template
        )
        cmd = ["docker", "run", "--rm", "--name", container_name,
               "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges",
               "--memory", f"{self.memory_mb}m",
               "--cpus", str(self.cpus),
               "--pids-limit", str(self.pids_limit)]
        if self.read_only:
            cmd.append("--read-only")
            if self.tmpfs:
                cmd += ["--tmpfs", self.tmpfs]
        if self.user:
            cmd += ["--user", self.user]
        if self.network_isolation:
            cmd += ["--network", "none"]   # CTF 模式网络隔离（硬约束 C7）
        return cmd + [self.image, "sh", "-c", inner]

    async def run(self, spec: ToolSpec, params: dict[str, Any]) -> ToolResult:
        self._validate_params(spec, params)
        container_name = f"spiritscout-{uuid.uuid4().hex[:12]}"
        wrapped = ToolSpec(
            name=f"docker:{spec.name}", kind=ToolKind.DOCKER, description=spec.description,
            risk=spec.risk, timeout_seconds=spec.timeout_seconds,
            command_template=self._docker_cmd(spec, params, container_name),
            parser=spec.parser,
        )
        result = await LocalExecutor.run(self, wrapped, params={})
        if result.timed_out:
            # 回滚保护：docker CLI 进程被杀 ≠ 容器停止；显式 kill 防残留
            await self._kill_container(container_name)
        return result

    async def _kill_container(self, name: str) -> None:
        """超时兜底：尽力 kill 命名容器（失败仅留痕，不掩盖原超时结果）。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "kill", name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=10)
            self.audit.log("tool.docker_kill", "executor", {"container": name})
        except (OSError, TimeoutError) as exc:
            self.audit.log("tool.docker_kill_failed", "executor",
                           {"container": name, "error": str(exc)[:200]})


def docker_available() -> bool:
    """探测 Docker 守护进程可用性（runtime 装配门：不可用则显式报错而非静默降级）。"""
    import shutil
    import subprocess
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(["docker", "info"], capture_output=True, timeout=10,
                              check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0
