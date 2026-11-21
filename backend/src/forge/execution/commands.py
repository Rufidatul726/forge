import asyncio
import logging
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_TIMEOUT_SECONDS = 300.0
DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024
MAX_ARGUMENTS = 64
MAX_ARGUMENT_BYTES = 16 * 1024
MAX_TOTAL_ARGUMENT_BYTES = 64 * 1024
TERMINATION_GRACE_SECONDS = 1.0

_DEFAULT_ALLOWED_EXECUTABLES = frozenset(
    {"git", "npm", "node", "python", "python3", "pytest", "ruff", "uv"}
)
_ENVIRONMENT_ALLOWLIST = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
)
logger = logging.getLogger("forge.execution")


class CommandError(Exception):
    """Base exception for controlled command execution."""


class CommandValidationError(CommandError):
    """The command request is malformed or exceeds configured limits."""


class CommandPolicyError(CommandError):
    """The requested executable is not permitted by the command policy."""


class CommandExecutionError(CommandError):
    """An approved command could not be started."""


class CommandPermissionDeniedError(CommandError):
    """The requested command was not explicitly approved."""


@dataclass(frozen=True)
class CommandProposal:
    argv: tuple[str, ...] = field(repr=False)
    executable: str
    executable_path: Path
    workspace_root: Path
    working_directory: Path
    timeout_seconds: float


@dataclass(frozen=True)
class CommandResult:
    executable: str
    working_directory: str
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    duration_ms: float


@dataclass(frozen=True)
class _CapturedOutput:
    content: bytes
    truncated: bool


class CommandRunner:
    """Run explicitly approved development commands without invoking a shell."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        allowed_executables: frozenset[str] = _DEFAULT_ALLOWED_EXECUTABLES,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        try:
            root = workspace_root.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise CommandValidationError("Workspace root cannot be resolved") from exc
        if not root.is_dir():
            raise CommandValidationError("Workspace root must be a directory")
        if max_output_bytes <= 0:
            raise ValueError("Command output limit must be positive")
        self.workspace_root = root
        self.allowed_executables = frozenset(
            name.casefold() for name in allowed_executables
        )
        self.max_output_bytes = max_output_bytes

    async def run(
        self,
        argv: Sequence[str],
        *,
        approve: Callable[[CommandProposal], bool],
        cwd: str | Path = ".",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> CommandResult:
        arguments = self._validate_arguments(argv)
        if not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise CommandValidationError(
                "Command timeout must be greater than 0 and at most "
                f"{MAX_TIMEOUT_SECONDS:g} seconds"
            )
        executable = self._resolve_executable(arguments[0])
        working_directory = self._resolve_working_directory(cwd)
        proposal = CommandProposal(
            argv=tuple(arguments),
            executable=Path(executable).name,
            executable_path=Path(executable),
            workspace_root=self.workspace_root,
            working_directory=working_directory,
            timeout_seconds=timeout_seconds,
        )
        if approve(proposal) is not True:
            logger.warning(
                "command_denied root=%s executable=%s cwd=%s",
                self.workspace_root,
                proposal.executable,
                working_directory.relative_to(self.workspace_root),
            )
            raise CommandPermissionDeniedError("Command was not approved")

        return await self._execute(proposal, executable)

    async def _execute(
        self, proposal: CommandProposal, executable: str
    ) -> CommandResult:
        started_at = time.perf_counter()
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in _ENVIRONMENT_ALLOWLIST
        }
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["UV_OFFLINE"] = "1"
        creation_options: dict[str, object] = {}
        if os.name == "nt":
            creation_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            creation_options["start_new_session"] = True

        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                *proposal.argv[1:],
                cwd=proposal.working_directory,
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **creation_options,
            )
        except OSError as exc:
            logger.warning(
                "command_start_failed root=%s executable=%s error_type=%s",
                self.workspace_root,
                proposal.executable,
                type(exc).__name__,
            )
            raise CommandExecutionError(
                "Approved command could not be started"
            ) from exc

        process_wait_task = asyncio.create_task(process.wait())
        stdout_task = asyncio.create_task(self._capture(process.stdout))
        stderr_task = asyncio.create_task(self._capture(process.stderr))
        timed_out = False
        try:
            _, pending = await asyncio.wait(
                {process_wait_task, stdout_task, stderr_task},
                timeout=proposal.timeout_seconds,
            )
            if pending:
                timed_out = True
                await self._terminate(process)
            _, stdout, stderr = await asyncio.gather(
                process_wait_task,
                stdout_task,
                stderr_task,
            )
        except asyncio.CancelledError:
            await asyncio.shield(self._terminate(process))
            await asyncio.shield(
                asyncio.gather(process_wait_task, stdout_task, stderr_task)
            )
            duration_ms = (time.perf_counter() - started_at) * 1000
            logger.warning(
                "command_cancelled root=%s executable=%s cwd=%s duration_ms=%.2f",
                self.workspace_root,
                proposal.executable,
                proposal.working_directory.relative_to(self.workspace_root),
                duration_ms,
            )
            raise

        duration_ms = (time.perf_counter() - started_at) * 1000
        result = CommandResult(
            executable=proposal.executable,
            working_directory=proposal.working_directory.relative_to(self.workspace_root).as_posix()
            or ".",
            exit_code=process.returncode,
            stdout=stdout.content.decode("utf-8", errors="replace"),
            stderr=stderr.content.decode("utf-8", errors="replace"),
            stdout_truncated=stdout.truncated,
            stderr_truncated=stderr.truncated,
            timed_out=timed_out,
            duration_ms=duration_ms,
        )
        logger.info(
            "command_completed root=%s executable=%s cwd=%s exit_code=%s timed_out=%s "
            "stdout_bytes=%d stderr_bytes=%d output_truncated=%s duration_ms=%.2f",
            self.workspace_root,
            result.executable,
            result.working_directory,
            result.exit_code,
            result.timed_out,
            len(stdout.content),
            len(stderr.content),
            stdout.truncated or stderr.truncated,
            result.duration_ms,
        )
        return result

    async def _capture(self, stream: asyncio.StreamReader | None) -> _CapturedOutput:
        if stream is None:
            return _CapturedOutput(content=b"", truncated=False)

        chunks: list[bytes] = []
        size = 0
        truncated = False
        while chunk := await stream.read(64 * 1024):
            remaining = self.max_output_bytes - size
            if remaining > 0:
                chunks.append(chunk[:remaining])
                size += min(len(chunk), remaining)
            if len(chunk) > remaining:
                truncated = True
        return _CapturedOutput(content=b"".join(chunks), truncated=truncated)

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if os.name == "nt":
            await self._terminate_windows_tree(process)
            return

        if process.returncode is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return

        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return

        try:
            await asyncio.wait_for(process.wait(), timeout=TERMINATION_GRACE_SECONDS)
        except TimeoutError:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()

    async def _terminate_windows_tree(
        self, process: asyncio.subprocess.Process
    ) -> None:
        system_root = os.environ.get("SYSTEMROOT")
        taskkill = (
            Path(system_root) / "System32" / "taskkill.exe"
            if system_root
            else None
        )
        tree_terminated = False
        if taskkill is not None and taskkill.is_file():
            killer: asyncio.subprocess.Process | None = None
            try:
                killer = await asyncio.create_subprocess_exec(
                    str(taskkill),
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                await asyncio.wait_for(
                    killer.wait(), timeout=TERMINATION_GRACE_SECONDS
                )
                tree_terminated = killer.returncode == 0
            except (OSError, TimeoutError) as exc:
                logger.warning(
                    "command_tree_termination_failed pid=%d error_type=%s",
                    process.pid,
                    type(exc).__name__,
                )
                if killer is not None and killer.returncode is None:
                    killer.kill()
                    await killer.wait()
        else:
            logger.warning("command_tree_termination_unavailable pid=%d", process.pid)

        if not tree_terminated:
            try:
                os.kill(process.pid, signal.CTRL_BREAK_EVENT)
            except OSError:
                pass
        if process.returncode is None:
            process.kill()
        await process.wait()

    @staticmethod
    def _validate_arguments(argv: Sequence[str]) -> list[str]:
        if isinstance(argv, (str, bytes)) or not argv or len(argv) > MAX_ARGUMENTS:
            raise CommandValidationError(
                f"Command must contain between 1 and {MAX_ARGUMENTS} separate arguments"
            )
        arguments = list(argv)
        if any(
            not isinstance(argument, str) or "\x00" in argument
            for argument in arguments
        ):
            raise CommandValidationError("Command arguments must be valid strings")
        try:
            argument_sizes = [len(argument.encode("utf-8")) for argument in arguments]
        except UnicodeEncodeError as exc:
            raise CommandValidationError(
                "Command arguments must be valid UTF-8 strings"
            ) from exc
        if any(size > MAX_ARGUMENT_BYTES for size in argument_sizes):
            raise CommandValidationError("A command argument exceeds the size limit")
        if sum(argument_sizes) > MAX_TOTAL_ARGUMENT_BYTES:
            raise CommandValidationError(
                "Command arguments exceed the total size limit"
            )
        if not arguments[0].strip() or arguments[0] != arguments[0].strip():
            raise CommandValidationError(
                "Command executable must not be empty or contain surrounding whitespace"
            )
        return arguments

    def _resolve_executable(self, name: str) -> str:
        path = PureWindowsPath(name)
        if "/" in name or "\\" in name or path.drive or path.is_absolute():
            raise CommandPolicyError(
                "Executable paths are not permitted; use an allowed command name"
            )
        command_name = Path(name).stem.casefold()
        if command_name not in self.allowed_executables:
            raise CommandPolicyError(
                "Executable is not allowed by the workspace command policy"
            )
        executable = shutil.which(name)
        if executable is None:
            raise CommandExecutionError("Allowed executable was not found on PATH")
        return str(Path(executable).resolve())

    def _resolve_working_directory(self, cwd: str | Path) -> Path:
        path_text = str(cwd)
        relative = Path(path_text)
        windows_path = PureWindowsPath(path_text)
        if (
            not path_text
            or "\x00" in path_text
            or relative.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or any(part == ".." for part in relative.parts)
        ):
            raise CommandValidationError(
                "Working directory must be relative to the workspace"
            )
        try:
            resolved = (self.workspace_root / relative).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise CommandValidationError("Working directory does not exist") from exc
        if not resolved.is_dir():
            raise CommandValidationError("Working directory must be a directory")
        try:
            resolved.relative_to(self.workspace_root)
        except ValueError as exc:
            raise CommandPolicyError(
                "Working directory must remain inside the workspace"
            ) from exc
        return resolved
