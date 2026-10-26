"""通过 bubblewrap 执行 shell 和 Python。

宿主文件系统只读，用户目录和控制目录遮蔽；附件只读，会话 sandbox 目录可写并持久化。
每次调用使用独立 tmpfs /tmp，清理环境变量，并限制运行时间和输出。网络保持可用。
bwrap 不可用时返回错误，不直接在宿主执行。"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import tempfile
import time
from pathlib import Path
from typing import Any

from spiritscout.tools.spec import ToolResult

_BWRAP = "bwrap"
_MIN_TIMEOUT = 5.0
_MAX_TIMEOUT = 300.0
_READ_CHUNK = 65536
_LOG_ROTATE_BYTES = 10 * 1024 * 1024    # 单日志超限轮转（保留上一代 .old）


class _RotatingLog:
    """写入中按字节轮转，两代有界；stdout/stderr 在同一事件循环共用。"""

    def __init__(self, path: Path):
        self.path = path
        self.previous = path.with_name(path.stem + ".old.log")
        self.limit = max(1, _LOG_ROTATE_BYTES)
        self.rotated = False
        self._fh = path.open("ab", buffering=0)
        self._size = path.stat().st_size
        if self._size > self.limit:
            # 旧版本可能留下超大日志，只保留尾窗，不全额读入内存。
            with path.open("rb") as source:
                source.seek(-self.limit, os.SEEK_END)
                self.previous.write_bytes(source.read(self.limit))
            self._fh.truncate(0)
            self._size = 0
            self.rotated = True

    def write(self, chunk: bytes) -> None:
        data = memoryview(chunk)
        while data:
            if self._size >= self.limit:
                self._fh.close()
                self.path.replace(self.previous)
                self._fh = self.path.open("wb", buffering=0)
                self._size = 0
                self.rotated = True
            take = min(len(data), self.limit - self._size)
            written = self._fh.write(data[:take])
            if not written:
                raise OSError("日志写入未完成")
            self._size += written
            data = data[written:]

    def close(self) -> None:
        self._fh.close()


def sandbox_available() -> bool:
    """bwrap 可用性探测（工具调用与装配提示共用）。"""
    return shutil.which(_BWRAP) is not None


def _project_venv() -> Path | None:
    """项目自带虚拟环境（工具库全部封装于此，不依赖用户系统安装）。

    沙箱路径：src/spiritscout/tools/sandbox.py → 项目根/.venv。
    """
    root = Path(__file__).resolve().parents[3]
    venv = root / ".venv"
    return venv if (venv / "bin" / "python3").exists() or \
        (venv / "bin" / "python").exists() else None


def _venv_site_dirs(venv: Path) -> list[str]:
    return [str(p) for p in sorted(venv.glob("lib/python*/site-packages"))]


def _build_argv(workspace: Path, rundir: Path, inner: list[str], *,
                guard_root: Path | None = None) -> list[str]:
    """bwrap 参数：整机只读 + 控制面遮蔽 + 产物可写/附件只读 + 干净环境。

    工具来源：项目内 .venv（只读回挂并入 PATH/PYTHONPATH）——用户主目录、
    ~/.local、/usr/local 等私有安装层一律不进沙箱，智能体工具能力来自
    项目封装与发行版基础层。
    """
    home = Path.home()
    argv = [
        _BWRAP,
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--unshare-pid",               # 不通过宿主 /proc/<pid>/root 绕过文件遮蔽
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--tmpfs", str(home),          # 主目录遮蔽：.ssh / 密钥库不可见
        # 宿主私有安装层遮蔽：/usr/local 是手工安装落点（用户自装工具、
        # 独立分发的二进制、私有库），整层遮蔽（bin 内 sbin 是符号链接，
        # 需整目录替换而非逐项挂载）
        "--tmpfs", "/usr/local",
    ]
    # 控制面根目录整层遮蔽（Web 会话：基础工作区，vault 密钥库/兄弟会话/
    # 审计都在里面）。只遮蔽会话自身目录时，同级会话与 vault.key/vault.db
    # 在沙箱内照样可读（密钥与密文同处可读＝供应商 Key 全量可解密）。
    # 置于 .venv 回挂之前：会话工作区含项目根时不把工具层一起盖掉
    if (guard_root is not None and str(guard_root) != "/"
            and guard_root != workspace and guard_root.is_dir()
            and workspace.is_relative_to(guard_root)):
        argv += ["--tmpfs", str(guard_root)]
    venv = _project_venv()
    path_dirs = ["/usr/bin", "/bin"]
    site_dirs: list[str] = []
    if venv is not None:
        argv += ["--ro-bind", str(venv), str(venv)]
        path_dirs = [str(venv / "bin"), *path_dirs]
        site_dirs = _venv_site_dirs(venv)
    # 整个控制目录不可见，只开放约定的产物目录；同样覆盖位于 /tmp
    # 或主目录之外的工作区，不能仅靠遮蔽 execution.key 保护数据库。
    argv += ["--tmpfs", str(workspace), "--bind", str(rundir), str(rundir)]
    for name in ("attachments", "artifacts"):
        directory = workspace / name
        if directory.is_dir():
            argv += ["--ro-bind" if name == "attachments" else "--bind",
                     str(directory), str(directory)]
    # 空控制目录本身也只读，不能伪造同名控制文件；不递归改变产物挂载。
    argv += ["--remount-ro", str(workspace)]
    argv += [
        "--chdir", str(rundir),
        "--die-with-parent",
        "--clearenv",
        "--setenv", "PATH", ":".join(path_dirs),
        "--setenv", "HOME", str(rundir),
        "--setenv", "LANG", "C.UTF-8",
    ]
    if site_dirs:
        argv += ["--setenv", "PYTHONPATH", ":".join(site_dirs)]
    return [*argv, *inner]


async def _read_capped(stream: asyncio.StreamReader | None,
                       cap: int, sink=None) -> tuple[bytes, int]:
    """流式读取并限幅：完整内容逐块写入 sink（落盘，过程不丢），
    内存只保留头尾，返回（保留字节, 实际总量）。"""
    if stream is None:
        return b"", 0
    head_cap = cap * 3 // 4
    tail_cap = cap - head_cap
    head = bytearray()
    tail = bytearray()
    total = 0
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if sink is not None:
            try:
                sink.write(chunk)
            except OSError:  # 落盘失败不影响执行与限幅读取
                sink = None
        if len(head) < head_cap:
            take = min(head_cap - len(head), len(chunk))
            head.extend(chunk[:take])
            chunk = chunk[take:]
        if chunk and tail_cap:
            tail.extend(chunk)
            if len(tail) > tail_cap:
                del tail[:-tail_cap]
    return bytes(head + tail), total


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """杀整个进程组（bwrap 与沙箱内全部子进程）。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _decode(data: bytes, total: int, cap: int) -> str:
    """解码限幅输出；被截断时在保留的头尾之间标明丢弃量。"""
    if total <= cap:
        return data.decode(errors="replace")
    head_cap = cap * 3 // 4
    dropped = total - len(data)
    return (data[:head_cap].decode(errors="replace")
            + f"\n...[输出超限，已丢弃 {dropped} 字节]...\n"
            + data[head_cap:].decode(errors="replace"))


async def sandbox_exec(
    code: str,
    *,
    language: str = "shell",
    timeout: float = 60.0,
    workspace_root: str | None = None,
    workspace_scope: str | None = None,
    guard_root: str | None = None,
    log_name: str = "last_output",
    max_output_chars: int = 20000,
    **_: Any,
) -> ToolResult:
    """沙箱执行：language=shell 跑 ``bash -c``；language=python 落盘后 ``python3`` 运行。

    ok 语义：进程正常结束（任何退出码）即 ok=True——退出码非零是结果不是
    工具故障；只有沙箱不可用 / 启动失败 / 超时被终止才 ok=False。
    workspace_scope：工作目录作用域（调用方传题级 id 等）。非空时工作目录
    细分为 ``sandbox/<scope>/``——多题并行时各题产物、脚本与日志互不覆盖；
    缺省时沿用会话级 ``sandbox/``（SRC 等单目标场景不受影响）。
    """
    started = time.perf_counter()
    code = str(code or "")
    if not code.strip():
        return ToolResult(tool="sandbox_exec", ok=False, returncode=-1,
                          error="执行内容为空")
    if not sandbox_available():
        return ToolResult(tool="sandbox_exec", ok=False, returncode=-1, error=(
            "沙箱不可用：未找到 bubblewrap（bwrap）。请安装 bubblewrap 后重试"
            "（不静默降级为宿主直跑）。"))
    if not workspace_root:
        return ToolResult(tool="sandbox_exec", ok=False, returncode=-1,
                          error="缺少工作区配置（workspace_root）")
    try:
        timeout = max(_MIN_TIMEOUT, min(float(timeout), _MAX_TIMEOUT))
    except (TypeError, ValueError):
        timeout = 60.0
    lang = "python" if str(language).lower() == "python" else "shell"
    workspace = Path(workspace_root).resolve()
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        # 不允许旧脚本留下的目录符号链接将控制目录重新挂进沙箱。
        for name in ("sandbox", "attachments", "artifacts"):
            if (workspace / name).is_symlink():
                raise OSError(f"沙箱共享目录不能是符号链接: {name}")
        # 会话持久工作目录：workspace_root 即会话工作区，跨调用保留产物，
        # 供后续回合与附件工具继续使用（/tmp 为每运行 tmpfs，不持久）；
        # 题级作用域在会话工作区内再细分一层（多题并行互不覆盖）。
        rundir = workspace / "sandbox"
        if workspace_scope:
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(workspace_scope)).strip("._-")[:80]
            if safe:
                rundir = rundir / safe
        rundir.mkdir(parents=True, exist_ok=True)
        (workspace / "artifacts").mkdir(exist_ok=True)
    except OSError as exc:
        return ToolResult(tool="sandbox_exec", ok=False, returncode=-1,
                          error=f"沙箱工作区不可用: {exc}")
    if lang == "python":
        script = rundir / f"run_{int(time.time() * 1000)}_{os.urandom(3).hex()}.py"
        script.write_text(code, encoding="utf-8")
        venv = _project_venv()
        py = str(venv / "bin" / "python3") if venv is not None else "python3"
        inner = [py, str(script)]
    else:
        inner = ["bash", "-c", code]

    guard = Path(guard_root).resolve() if guard_root else None
    argv = _build_argv(workspace, rundir, inner, guard_root=guard)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
    except OSError as exc:
        return ToolResult(tool="sandbox_exec", ok=False, returncode=-1,
                          error=f"沙箱启动失败: {exc}")

    cap = max(1000, int(max_output_chars))
    # 完整输出逐块落盘（工作目录内，跨调用可查）——上下文只进头尾摘要，
    # 过程不因截断而丢失；需要细节时模型用沙箱按需读取该文件
    log_name = Path(str(log_name)).name[:160] or "last_output"
    log_path = None
    try:
        # 同题分支和同回合多工具可能同名并发，返回的证据路径必须永久对应本次调用。
        fd, filename = tempfile.mkstemp(prefix=f"{log_name}-", suffix=".log", dir=rundir)
        os.close(fd)
        log_path = Path(filename)
        # 日志代际轮转：单文件超限就把上一代挪到 .old——完整过程保留最近
        # 两代，磁盘占用有界；模型读取的 output_file 始终指向当前代
        log_fh = _RotatingLog(log_path)
    except OSError:
        log_fh = None
        log_path = None
    out_task = asyncio.create_task(_read_capped(proc.stdout, cap, sink=log_fh))
    err_task = asyncio.create_task(_read_capped(proc.stderr, cap, sink=log_fh))
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        _kill_group(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            pass
    except BaseException:
        # 取消（会话停止）/异常路径同样不留活口：杀进程组、回收、关日志，
        # 然后原样抛出——停止后不得残留沙箱进程
        _kill_group(proc)
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(proc.wait(), timeout=5)
        out_task.cancel()
        err_task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.gather(out_task, err_task, return_exceptions=True)
        if log_fh is not None:
            log_fh.close()
        raise
    try:
        (out_b, out_total), (err_b, err_total) = await asyncio.wait_for(
            asyncio.gather(out_task, err_task), timeout=5)
    except TimeoutError:
        out_task.cancel()
        err_task.cancel()
        await asyncio.gather(out_task, err_task, return_exceptions=True)
        out_b = err_b = b""
        out_total = err_total = 0
    finally:
        if log_fh is not None:
            log_fh.close()
    rc = proc.returncode if proc.returncode is not None else -1

    duration = (time.perf_counter() - started) * 1000
    stdout = _decode(out_b, out_total, max_output_chars)
    stderr = _decode(err_b, err_total, max_output_chars)
    return ToolResult(
        tool="sandbox_exec", ok=not timed_out, returncode=rc,
        stdout=stdout, stderr=stderr, timed_out=timed_out,
        parsed={"language": lang, "exit_code": rc,
                "duration_ms": round(duration, 1), "workdir": str(rundir),
                "output_file": str(log_path) if log_path else "",
                "output_rotated": bool(log_fh and log_fh.rotated),
                "previous_output_file": str(log_fh.previous)
                if log_fh and log_fh.previous.exists() else ""},
        error=(f"执行超时（{timeout:.0f}s），进程已终止" if timed_out else ""),
        duration_ms=duration,
    )
