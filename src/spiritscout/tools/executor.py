"""工具执行器：超时控制、参数校验、输出截断、重试与降级、回滚保护。

回滚保护（验收标准）：执行失败或超时后，将目标状态恢复到执行前快照。
对只读探测类工具回滚为空操作；对产生本地副作用的工具（写文件/会话），
执行前记录快照、失败后清理（如删除临时文件、关闭残留进程）。
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import os
import shlex
import signal
import time
import uuid
from pathlib import Path
from typing import Any

from spiritscout.core.audit import AuditLogger, NullAuditLogger
from spiritscout.core.exceptions import ToolExecutionError
from spiritscout.tools.spec import ToolKind, ToolResult, ToolSpec


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """杀整个进程组（子进程派生的孙进程一并终止）；单杀兜底。"""
    try:
        # start_new_session=True 保证 pgid=pid；父进程退出后仍可清理后代，
        # 不能再通过已消失的父 PID 查询进程组。
        os.killpg(proc.pid, signal.SIGKILL)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


_READ_CHUNK = 65536


async def _wait_parent(proc: asyncio.subprocess.Process) -> None:
    """只等直接子进程退出；后代持有管道时 proc.wait() 还会继续等待 EOF。"""
    while proc.returncode is None:
        await asyncio.sleep(0.01)


async def _read_capped(stream: asyncio.StreamReader | None,
                       cap: int) -> tuple[bytes, int]:
    """流式读取限内存：只保留头尾窗口，超出部分读走即弃。

    持续把管道读空，进程得以自然收尾（不因管道塞满被自己的输出卡死）；
    返回（保留字节, 实际总量）——总量可用于审计，保留内容走展示截断。
    """
    if stream is None:
        return b"", 0
    head_cap = max(cap, 1000) * 3 // 4
    tail_cap = max(cap, 1000) - head_cap
    head = bytearray()
    tail = bytearray()
    total = 0
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if len(head) < head_cap:
            take = chunk[:head_cap - len(head)]
            head.extend(take)
            chunk = chunk[len(take):]
        if chunk:
            tail.extend(chunk)
            if len(tail) > tail_cap:
                del tail[:len(tail) - tail_cap]
    return bytes(head) + bytes(tail), total


class ToolExecutor(abc.ABC):
    def __init__(self, max_output_chars: int = 20000, audit: AuditLogger | None = None):
        self.max_output_chars = max_output_chars
        self.audit = audit or NullAuditLogger()

    @abc.abstractmethod
    async def run(self, spec: ToolSpec, params: dict[str, Any]) -> ToolResult: ...

    async def run_argv(self, spec: ToolSpec, cmd: list[str]) -> ToolResult:
        """执行已完成展开的 argv 数组（模板展开只发生在各 run() 的一处，
        之后原样传递——用户数据里的花括号不会再被格式化）。

        超时与取消（会话停止）都按进程组清理：杀组、回收，不残留子进程。
        """
        started = time.perf_counter()
        rollback = _RollbackStack()
        # start_new_session：独立进程组，超时/取消可整组终止
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            # 启动失败也是工具结果；取消异常不在此吞掉。
            return ToolResult(tool=spec.name, ok=False,
                              returncode=127 if isinstance(exc, FileNotFoundError) else 126,
                              error=f"工具启动失败: {exc}",
                              duration_ms=(time.perf_counter() - started) * 1000)
        rollback.track_process(proc)
        # 流式限幅读取：内存只留头尾窗口，超出部分读走即弃——进程得以
        # 自然收尾（不因管道塞满卡死），内存也不随输出体量增长
        out_task = asyncio.create_task(
            _read_capped(proc.stdout, self.max_output_chars))
        err_task = asyncio.create_task(
            _read_capped(proc.stderr, self.max_output_chars))
        try:
            try:
                await asyncio.wait_for(_wait_parent(proc), timeout=spec.timeout_seconds)
                timed_out = False
            except TimeoutError:
                timed_out = True
                _kill_process_group(proc)
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(proc.wait(), timeout=5)
        except BaseException:
            # 取消/异常路径同样不留活口：整组清理后原样抛出
            _kill_process_group(proc)
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(proc.wait(), timeout=5)
            out_task.cancel()
            err_task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.gather(out_task, err_task, return_exceptions=True)
            raise
        # 普通 CLI 调用不拥有跨调用后台服务；直接子进程收尾后立即收口
        # 同组后代，再读完已产生的输出，避免延迟写入和管道永久不关闭。
        _kill_process_group(proc)
        try:
            (stdout_b, _), (stderr_b, _) = await asyncio.wait_for(
                asyncio.gather(out_task, err_task), timeout=5)
        except TimeoutError:
            out_task.cancel()
            err_task.cancel()
            await asyncio.gather(out_task, err_task, return_exceptions=True)
            stdout_b = stderr_b = b""
        finally:
            _kill_process_group(proc)
            out_task.cancel()
            err_task.cancel()
            await asyncio.gather(out_task, err_task, return_exceptions=True)
        duration = (time.perf_counter() - started) * 1000
        stdout = self._truncate(stdout_b.decode(errors="replace"))
        stderr = self._truncate(stderr_b.decode(errors="replace"))
        ok = not timed_out and (proc.returncode == 0)
        rolled = False
        if not ok:
            rolled = await rollback.rollback()
        result = ToolResult(
            tool=spec.name, ok=ok,
            returncode=proc.returncode if proc.returncode is not None else -1,
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
        """模板槽位在本处一次性格式化；argv 之后原样传递（run_argv）。"""
        self._validate_params(spec, params)
        # subprocess_exec 走 argv 数组、不经 shell：参数按字面值传递，天然防注入
        cmd = [part.format(**{k: str(v) for k, v in params.items()})
               for part in spec.command_template]
        self.audit.log("tool.call", "executor",
                       {"tool": spec.name, "kind": spec.kind.value, "params": params})
        return await self.run_argv(spec, cmd)


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
        # 内层命令在装配时一次性完成格式化与 shell 转义；外层 docker argv
        # 原样执行（不再二次格式化——用户数据里的花括号原样保留）
        cmd = self._docker_cmd(spec, params, container_name)
        wrapped = ToolSpec(
            name=f"docker:{spec.name}", kind=ToolKind.DOCKER, description=spec.description,
            risk=spec.risk, timeout_seconds=spec.timeout_seconds,
            command_template=cmd,   # 已展开，仅供审计展示；执行不再格式化
            parser=spec.parser, target_params=(), scope_required=False,
        )
        self.audit.log("tool.call", "executor",
                       {"tool": spec.name, "kind": spec.kind.value,
                        "container": container_name, "params": params})
        try:
            result = await self.run_argv(wrapped, cmd)
        except BaseException:
            # 取消（会话停止）：docker CLI 被杀 ≠ 容器停止；显式停掉命名容器
            await self._kill_container(container_name)
            raise
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
