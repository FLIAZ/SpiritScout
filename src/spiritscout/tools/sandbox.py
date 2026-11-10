"""通过 bubblewrap 执行 shell 和 Python。

宿主文件系统只读，用户目录和控制目录遮蔽；附件只读，会话 sandbox 目录可写并持久化。
每次调用清理环境变量，并限制运行时间和输出。网络保持可用。
默认 /tmp 为独立 tmpfs；合格轻量 CTF/AWDP 调用使用题级持久 /tmp。
bwrap 不可用时返回错误，不直接在宿主执行。"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import hashlib
import os
import shlex
import shutil
import signal
import stat
import sys
import tempfile
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from spiritscout.core.models import RunMode
from spiritscout.platform_support import app_root, is_frozen
from spiritscout.tools.sandbox_paths import (
    open_directory,
    sandbox_directory,
    sandbox_tmp_directory,
)
from spiritscout.tools.spec import ToolResult

_BWRAP = "bwrap"
_MIN_TIMEOUT = 5.0
_MAX_TIMEOUT = 300.0
_READ_CHUNK = 65536
_LOG_ROTATE_BYTES = 10 * 1024 * 1024    # 单日志超限轮转（保留上一代 .old）
# 仅后台作业管理器设置；不能通过模型工具参数关闭普通命令的超时。
background_output: ContextVar[Any] = ContextVar("sandbox_background_output", default=None)
# 离线回归由可信调用方设置；不暴露为模型工具参数，也不改变普通赛题网络策略。
offline_execution: ContextVar[bool] = ContextVar("sandbox_offline_execution", default=False)

class _RotatingLog:
    """写入中按字节轮转，两代有界；stdout/stderr 在同一事件循环共用。"""

    def __init__(self, path: Path):
        self.path = path
        self.previous = path.with_name(path.stem + ".old.log")
        self.limit = max(1, _LOG_ROTATE_BYTES)
        self.rotated = False
        self.digest = hashlib.sha256()
        self.previous_sha256 = ''
        self.written_bytes = 0
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
            self.previous_sha256 = hashlib.sha256(self.previous.read_bytes()).hexdigest()
        elif self._size:
            with path.open('rb') as existing:
                while chunk := existing.read(_READ_CHUNK):
                    self.digest.update(chunk)

    def write(self, chunk: bytes) -> None:
        data = memoryview(chunk)
        while data:
            if self._size >= self.limit:
                self._fh.close()
                self.path.replace(self.previous)
                self.previous_sha256 = self.digest.hexdigest()
                self._fh = self.path.open("wb", buffering=0)
                self.digest = hashlib.sha256()
                self._size = 0
                self.rotated = True
            take = min(len(data), self.limit - self._size)
            written = self._fh.write(data[:take])
            if not written:
                raise OSError("日志写入未完成")
            self._size += written
            self.written_bytes += written
            self.digest.update(data[:written])
            data = data[written:]

    def close(self) -> None:
        self._fh.close()


def sandbox_available() -> bool:
    """bwrap 可用性探测（工具调用与装配提示共用）。"""
    return sys.platform.startswith("linux") and shutil.which(_BWRAP) is not None


def _project_venv() -> Path | None:
    """项目自带虚拟环境（工具库全部封装于此，不依赖用户系统安装）。

    源码运行取仓库根/.venv；冻结构建取可执行文件旁/.venv，供 portable
    部署自行放置工具层，不继承启动终端里已激活的无关虚拟环境。
    """
    root = app_root()
    candidates: list[Path] = []
    if not is_frozen() and os.environ.get("VIRTUAL_ENV"):
        candidates.append(Path(os.environ["VIRTUAL_ENV"]))
    candidates.append(root / ".venv")
    return next((venv for venv in candidates if (venv / "bin" / "python3").exists()
                 or (venv / "bin" / "python").exists()), None)


def _venv_site_dirs(venv: Path) -> list[str]:
    return [str(p) for p in sorted(venv.glob("lib/python*/site-packages"))]


def _cuda_devices(root: Path = Path('/dev')) -> list[Path]:
    """只开放当前用户可访问的 CUDA 字符设备，不开放整个宿主 /dev 或显示控制设备。"""
    candidates = [root / name for name in ('nvidiactl', 'nvidia-uvm', 'nvidia-uvm-tools')]
    candidates += [path for path in root.glob('nvidia[0-9]*') if path.name[6:].isdecimal()]
    candidates += [path for path in (root / 'nvidia-caps').glob('nvidia-cap[0-9]*')
                   if path.name[len('nvidia-cap'):].isdecimal()]
    result = []
    for path in candidates:
        try:
            if stat.S_ISCHR(path.lstat().st_mode) and os.access(path, os.R_OK):
                result.append(path)
        except OSError:
            continue
    return sorted(result)


def _build_argv(workspace: Path, rundir: Path, inner: list[str], *,
                guard_root: Path | None = None,
                persistent_tmp: Path | None = None,
                directory_fds: dict[Path, int] | None = None,
                proxy_env: dict[str, str] | None = None) -> list[str]:
    """bwrap 参数：整机只读 + 控制面遮蔽 + 产物可写/附件只读 + 干净环境。

    工具来源：项目内 .venv（只读回挂并入 PATH/PYTHONPATH）——用户主目录、
    ~/.local、/usr/local 等私有安装层一律不进沙箱，智能体工具能力来自
    项目封装与发行版基础层。
    """
    def source(path: Path) -> str:
        # 目录身份已由宿主固定；不要在 bwrap 启动时再次解析可被替换的路径。
        fd = (directory_fds or {}).get(path)
        return f"/proc/self/fd/{fd}" if fd is not None else str(path)

    home = Path.home()
    argv = [
        _BWRAP,
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        # 云端/本地 LLM 与赛题计算设备独立：普通、轻量工具都可以使用已有 CUDA。
        *(argument for device in _cuda_devices() for argument in ('--dev-bind', str(device), str(device))),
        "--unshare-pid",               # 不通过宿主 /proc/<pid>/root 绕过文件遮蔽
        *(("--unshare-net",) if offline_execution.get() else ()),
        "--proc", "/proc",
        # 轻量 × CTF：/tmp 换成题级持久目录（tmpfs 每调用即焚会让小模型
        # 陷入"解压→下次消失→再解压"的原地循环，实测反复打转）。
        *(('--bind', source(persistent_tmp), '/tmp') if persistent_tmp is not None
          else ("--tmpfs", "/tmp")),
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
    argv += ["--tmpfs", str(workspace), "--bind", source(rundir), str(rundir)]
    for name in ("attachments", "artifacts", "http-responses", "traffic-ca"):
        if name == "traffic-ca" and not proxy_env:
            continue
        directory = workspace / name
        if directory in (directory_fds or {}) or directory.is_dir():
            argv += ["--bind" if name == "artifacts" else "--ro-bind",
                     source(directory), str(directory)]
    # 空控制目录本身也只读，不能伪造同名控制文件；不递归改变产物挂载。
    argv += ["--remount-ro", str(workspace)]
    argv += [
        "--chdir", str(rundir),
        "--die-with-parent",
        "--clearenv",
        "--setenv", "PATH", ":".join(path_dirs),
        "--setenv", "HOME", str(rundir),
        "--setenv", "LANG", "C.UTF-8",
        # TERM 留空时 gdb/pwndbg 等先报 terminfo 告警再干活——徒增噪音
        "--setenv", "TERM", "xterm",
    ]
    if site_dirs:
        argv += ["--setenv", "PYTHONPATH", ":".join(site_dirs)]
    for key, value in (proxy_env or {}).items():
        argv += ["--setenv", key, value]
    if background_output.get() is not None:
        argv += ["--setenv", "PYTHONUNBUFFERED", "1"]
    socket_path = (proxy_env or {}).get('SPIRITSCOUT_EGRESS_SOCKET')
    if socket_path and not offline_execution.get():
        from spiritscout.tools.egress_isolation import relay_command

        mounted = '/tmp/spiritscout-egress.sock'
        argv += ['--unshare-net', '--ro-bind', socket_path, mounted]
        inner = relay_command(inner, mounted)
    return [*argv, *inner]


async def _read_capped(stream: asyncio.StreamReader | None,
                       cap: int, sink=None, observe=None) -> tuple[bytes, int]:
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
        if observe is not None:
            observe(chunk)
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
    _run_mode: RunMode | None = None,
    _lightweight_local: bool = False,
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
            "沙箱不可用：需要 Linux 与 bubblewrap（bwrap）。Windows/macOS 请使用 Docker 部署，"
            "Linux 请安装 bubblewrap 并确认命名空间权限；不会直接在宿主执行"
            "（不静默降级为宿主直跑）。"))
    if not workspace_root:
        return ToolResult(tool="sandbox_exec", ok=False, returncode=-1,
                          error="缺少工作区配置（workspace_root）")
    if background_output.get() is not None:
        timeout = None
    else:
        try:
            timeout = max(_MIN_TIMEOUT, min(float(timeout), _MAX_TIMEOUT))
        except (TypeError, ValueError):
            timeout = 60.0
    lang = "python" if str(language).lower() == "python" else "shell"
    lightweight = _lightweight_local and _run_mode in (RunMode.CTF, RunMode.AWDP)
    if lightweight:
        with contextlib.suppress(TypeError, ValueError):
            max_output_chars = min(int(max_output_chars), 4000)
    workspace = Path(workspace_root).resolve()
    from spiritscout.traffic.proxy import tool_environment

    proxy_env = await tool_environment(workspace)
    with contextlib.ExitStack() as directories:
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            relative = sandbox_directory(workspace_scope)
            rundir = workspace / relative
            directory_fds = {rundir: directories.enter_context(
                open_directory(workspace, relative, create=True))}
            for name in ("attachments", "artifacts", "http-responses"):
                try:
                    fd = directories.enter_context(open_directory(
                        workspace, Path(name), create=name == "artifacts"))
                except FileNotFoundError:
                    continue
                directory_fds[workspace / name] = fd
            persistent_tmp = None
            if lightweight:
                temporary = sandbox_tmp_directory(workspace_scope)
                persistent_tmp = workspace / temporary
                directory_fds[persistent_tmp] = directories.enter_context(
                    open_directory(workspace, temporary, create=True))
            if proxy_env:
                directory_fds[workspace / "traffic-ca"] = directories.enter_context(
                    open_directory(workspace, Path("traffic-ca")))
        except (OSError, ValueError) as exc:
            return ToolResult(tool="sandbox_exec", ok=False, returncode=-1,
                              error=f"沙箱工作区不可用（目录不能是符号链接）: {exc}")
        return await _execute_sandbox(
            code, lang, timeout, workspace, rundir, persistent_tmp, directory_fds,
            guard_root, log_name, max_output_chars, started, proxy_env=proxy_env)


async def _execute_sandbox(code: str, lang: str, timeout: float | None, workspace: Path,
                           rundir: Path, persistent_tmp: Path | None,
                           directory_fds: dict[Path, int], guard_root: str | None,
                           log_name: str, max_output_chars: int, started: float,
                           proxy_env: dict[str, str] | None = None) -> ToolResult:
    directory = Path(f"/proc/self/fd/{directory_fds[rundir]}")
    pipeline_name = None
    if lang == "python":
        filename = f"run_{int(time.time() * 1000)}_{os.urandom(6).hex()}.py"
        fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory_fds[rundir])
        with os.fdopen(fd, "w", encoding="utf-8") as script:
            script.write(code)
        venv = _project_venv()
        py = str(venv / "bin" / "python3") if venv is not None else "python3"
        inner = [py, str(rundir / filename)]
    else:
        if persistent_tmp is not None:
            pipeline_name = f".pipeline-{os.urandom(8).hex()}"
            handler = ('_ss_rc=$? _ss_codes=("${PIPESTATUS[@]}"); '
                       'printf "%s" "${_ss_codes[*]}" > ' + shlex.quote(str(rundir / pipeline_name))
                       + '; exit "$_ss_rc"')
            inner = ["bash", "-o", "pipefail", "-c", "trap " + shlex.quote(handler) + " EXIT\n" + code]
        else:
            inner = ["bash", "-c", code]

    # bwrap 需要继承描述符完成挂载，但执行的程序不能据此访问宿主父目录。
    close_fds = " ".join(f"{fd}<&-" for fd in directory_fds.values())
    inner = ["bash", "-c", f'exec {close_fds}; exec "$@"', "sandbox", *inner]
    guard = Path(guard_root).resolve() if guard_root else None
    argv = _build_argv(workspace, rundir, inner, guard_root=guard,
                       persistent_tmp=persistent_tmp, directory_fds=directory_fds, proxy_env=proxy_env)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, pass_fds=tuple(directory_fds.values()))
    except OSError as exc:
        return ToolResult(tool="sandbox_exec", ok=False, returncode=-1,
                          error=f"沙箱启动失败: {exc}")

    cap = max(1000, int(max_output_chars))
    # 完整输出逐块落盘（工作目录内，跨调用可查）——上下文只进头尾摘要，
    # 过程不因截断而丢失；需要细节时模型用沙箱按需读取该文件
    log_name = Path(str(log_name)).name[:160] or "last_output"
    observer = background_output.get()
    if observer is not None:
        observer("started", proc.pid)
    log_path = None
    try:
        # 同题分支和同回合多工具可能同名并发，返回的证据路径必须永久对应本次调用。
        fd, filename = tempfile.mkstemp(prefix=f"{log_name}-", suffix=".log", dir=directory)
        os.close(fd)
        log_path = Path(filename)
        # 日志代际轮转：单文件超限就把上一代挪到 .old——完整过程保留最近
        # 两代，磁盘占用有界；模型读取的 output_file 始终指向当前代
        log_fh = _RotatingLog(log_path)
    except OSError:
        log_fh = None
        log_path = None
    observe = background_output.get()
    streams = {name: {"digest": hashlib.sha256(), "decoder": codecs.getincrementaldecoder('utf-8')('strict'),
                      "lossy": False, "nul": False, "head": bytearray(), "bytes": 0}
               for name in ('stdout', 'stderr')}

    def capture(name: str, chunk: bytes) -> None:
        state = streams[name]
        state['digest'].update(chunk)
        state['bytes'] += len(chunk)
        state['nul'] |= b'\x00' in chunk
        state['head'].extend(chunk[:max(0, 128 - len(state['head']))])
        if not state['lossy']:
            try:
                state['decoder'].decode(chunk)
            except UnicodeDecodeError:
                state['lossy'] = True
        if observe is not None:
            observe(name, chunk)

    out_task = asyncio.create_task(_read_capped(proc.stdout, cap, sink=log_fh,
        observe=lambda data: capture("stdout", data)))
    err_task = asyncio.create_task(_read_capped(proc.stderr, cap, sink=log_fh,
        observe=lambda data: capture("stderr", data)))
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
    capture_complete = True
    try:
        (out_b, out_total), (err_b, err_total) = await asyncio.wait_for(
            asyncio.gather(out_task, err_task), timeout=5)
    except TimeoutError:
        capture_complete = False
        out_task.cancel()
        err_task.cancel()
        await asyncio.gather(out_task, err_task, return_exceptions=True)
        out_b = err_b = b""
        out_total = err_total = 0
    finally:
        if log_fh is not None:
            log_fh.close()
    rc = proc.returncode if proc.returncode is not None else -1
    pipeline_statuses = []
    if pipeline_name:
        try:
            fd = os.open(pipeline_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory_fds[rundir])
            try:
                if stat.S_ISREG(os.fstat(fd).st_mode) and os.fstat(fd).st_size <= 1024:
                    values = os.read(fd, 1024).decode().split()
                    if len(values) <= 128 and all(v.isdigit() and 0 <= int(v) <= 255 for v in values):
                        pipeline_statuses = [int(v) for v in values]
            finally:
                os.close(fd)
        except (OSError, UnicodeError):
            pass
        finally:
            with contextlib.suppress(OSError):
                os.unlink(pipeline_name, dir_fd=directory_fds[rundir])
    pipeline_partial = (not timed_out and rc == 141 and len(pipeline_statuses) > 1
                        and pipeline_statuses[-1] == 0 and out_total > 0 and not err_total
                        and all(code in (0, 141) for code in pipeline_statuses))

    duration = (time.perf_counter() - started) * 1000
    stdout = _decode(out_b, out_total, max_output_chars)
    stderr = _decode(err_b, err_total, max_output_chars)
    captures = {}
    for name, state in streams.items():
        if capture_complete and not state['lossy']:
            try:
                state['decoder'].decode(b'', final=True)
            except UnicodeDecodeError:
                state['lossy'] = True
        captures[name + '_capture'] = {"sha256": state['digest'].hexdigest(), "bytes": state['bytes'],
            "complete": capture_complete, "text_decode_lossy": state['lossy'], "contains_nul": state['nul'],
            "preview_truncated": state['bytes'] > cap, "head_hex": bytes(state['head']).hex()}
    return ToolResult(
        tool="sandbox_exec", ok=not timed_out, returncode=rc,
        stdout=stdout, stderr=stderr, timed_out=timed_out,
        parsed={"language": lang, "exit_code": rc,
                "duration_ms": round(duration, 1), "workdir": str(rundir),
                "output_file": str(rundir / log_path.name) if log_path else "",
                "output_sha256": log_fh.digest.hexdigest() if log_fh else '',
                "previous_output_sha256": log_fh.previous_sha256 if log_fh else '',
                "output_complete": bool(log_fh and capture_complete
                    and log_fh.written_bytes == sum(state['bytes'] for state in streams.values())),
                **captures,
                "tmp_persistent": persistent_tmp is not None,
                **({"pipeline_statuses": pipeline_statuses, "pipeline_partial": pipeline_partial}
                   if pipeline_name else {}),
                "output_rotated": bool(log_fh and log_fh.rotated),
                "previous_output_file": str(rundir / log_fh.previous.name)
                if log_fh and log_fh.previous.exists() else ""},
        error=(f"执行超时（{timeout:.0f}s），进程已终止" if timed_out else ""),
        duration_ms=duration,
    )
