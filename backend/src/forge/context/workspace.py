import difflib
import hashlib
import logging
import os
import re
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

MAX_FILE_BYTES = 1024 * 1024
MAX_LIST_LIMIT = 1000
MAX_SEARCH_LIMIT = 1000
MAX_FILES_SCANNED = 5000

_IGNORED_DIRECTORIES = {
    ".git",
    ".forge",
    ".hg",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".terraform",
    ".tox",
    ".turbo",
    ".venv",
    ".vite",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "vendor",
}
_SENSITIVE_DIRECTORIES = {
    ".aws",
    ".azure",
    ".gnupg",
    ".ssh",
    "secrets",
}
_SAFE_ENV_SUFFIXES = {".example", ".sample", ".template"}
_SENSITIVE_FILENAMES = {
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "known_hosts",
    "secrets.yaml",
    "secrets.yml",
    "secrets.json",
    "terraform.tfstate",
    "token.json",
}
_SENSITIVE_SUFFIXES = {
    ".crt",
    ".der",
    ".jks",
    ".key",
    ".keystore",
    ".p12",
    ".pfx",
    ".pem",
}
logger = logging.getLogger("forge.context")


class WorkspaceError(Exception):
    """Base exception for workspace access errors."""


class WorkspacePathError(WorkspaceError):
    """A path is invalid, excluded, or outside the workspace."""


class WorkspaceFileTooLargeError(WorkspaceError):
    """A requested file exceeds the read limit."""


class WorkspaceEncodingError(WorkspaceError):
    """A requested file is not valid UTF-8 text."""


class WorkspacePermissionDeniedError(WorkspaceError):
    """A requested workspace write was not explicitly approved."""


class WorkspaceConflictError(WorkspaceError):
    """The target changed after its write proposal was created."""


@dataclass(frozen=True)
class WriteProposal:
    workspace_root: Path
    path: str
    content: str = field(repr=False)
    diff: str = field(repr=False)
    is_new: bool
    previous_sha256: str | None
    proposed_sha256: str


@dataclass(frozen=True)
class WriteResult:
    path: str
    changed: bool
    bytes_written: int
    previous_sha256: str | None
    current_sha256: str


@dataclass(frozen=True)
class FileList:
    paths: list[str]
    truncated: bool


@dataclass(frozen=True)
class FileMatch:
    path: str
    line_number: int
    line: str
    line_truncated: bool


@dataclass(frozen=True)
class SearchResults:
    matches: list[FileMatch]
    files_scanned: int
    files_skipped: int
    truncated: bool


@dataclass(frozen=True)
class WorkspaceContext:
    root: Path
    instructions: str | None
    files: FileList


class Workspace:
    """Read-only repository access constrained to a canonical workspace root."""

    def __init__(
        self,
        root: Path,
        *,
        max_file_bytes: int = MAX_FILE_BYTES,
        max_files_scanned: int = MAX_FILES_SCANNED,
    ) -> None:
        try:
            resolved_root = root.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorkspacePathError("Workspace root cannot be resolved") from exc
        if not resolved_root.is_dir():
            raise WorkspacePathError("Workspace root must be a directory")
        if max_file_bytes <= 0 or max_files_scanned <= 0:
            raise ValueError("Workspace limits must be positive")

        self.root = resolved_root
        self.max_file_bytes = max_file_bytes
        self.max_files_scanned = max_files_scanned

    def read_text(self, relative_path: str | Path) -> str:
        path = self._resolve_file(relative_path)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise WorkspacePathError("Workspace file cannot be accessed") from exc
        if size > self.max_file_bytes:
            raise WorkspaceFileTooLargeError("Workspace file exceeds the read limit")

        try:
            with path.open("rb") as file:
                content = file.read(self.max_file_bytes + 1)
        except OSError as exc:
            raise WorkspacePathError("Workspace file cannot be accessed") from exc
        if len(content) > self.max_file_bytes:
            raise WorkspaceFileTooLargeError("Workspace file exceeds the read limit")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceEncodingError("Workspace file is not valid UTF-8 text") from exc
        if "\x00" in text:
            raise WorkspaceEncodingError("Workspace file is not valid UTF-8 text")
        return text

    def write_text(
        self,
        relative_path: str | Path,
        content: str,
        *,
        approve: Callable[[WriteProposal], bool],
    ) -> WriteResult:
        proposal = self.propose_write(relative_path, content)
        if proposal.previous_sha256 == proposal.proposed_sha256:
            logger.info(
                "workspace_write_skipped root=%s path=%s reason=unchanged",
                self.root,
                proposal.path,
            )
            return WriteResult(
                path=proposal.path,
                changed=False,
                bytes_written=0,
                previous_sha256=proposal.previous_sha256,
                current_sha256=proposal.proposed_sha256,
            )
        if approve(proposal) is not True:
            logger.warning(
                "workspace_write_denied root=%s path=%s",
                self.root,
                proposal.path,
            )
            raise WorkspacePermissionDeniedError("Workspace write was not approved")

        target = self._resolve_write_target(proposal.path)
        current_content = self._read_optional_text(target)
        current_sha256 = self._sha256(current_content) if current_content is not None else None
        if current_sha256 != proposal.previous_sha256:
            logger.warning(
                "workspace_write_conflict root=%s path=%s",
                self.root,
                proposal.path,
            )
            raise WorkspaceConflictError("Workspace file changed after the write was proposed")

        encoded_content = proposal.content.encode("utf-8")
        previous_mode = stat.S_IMODE(target.stat().st_mode) if current_content is not None else None
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=".forge-write-",
                suffix=".tmp",
                dir=target.parent,
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(encoded_content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            if previous_mode is not None:
                os.chmod(temporary_path, previous_mode)
            os.replace(temporary_path, target)
        except OSError as exc:
            raise WorkspaceError("Workspace file could not be written") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        result = WriteResult(
            path=proposal.path,
            changed=True,
            bytes_written=len(encoded_content),
            previous_sha256=proposal.previous_sha256,
            current_sha256=proposal.proposed_sha256,
        )
        logger.info(
            "workspace_write_completed root=%s path=%s action=%s bytes_written=%d "
            "previous_sha256=%s current_sha256=%s",
            self.root,
            result.path,
            "created" if proposal.is_new else "updated",
            result.bytes_written,
            result.previous_sha256 or "none",
            result.current_sha256,
        )
        return result

    def propose_write(self, relative_path: str | Path, content: str) -> WriteProposal:
        if not isinstance(content, str):
            raise TypeError("Workspace write content must be text")
        if "\x00" in content:
            raise WorkspaceEncodingError("Workspace content must be valid UTF-8 text")
        try:
            encoded_content = content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise WorkspaceEncodingError("Workspace content must be valid UTF-8 text") from exc
        if len(encoded_content) > self.max_file_bytes:
            raise WorkspaceFileTooLargeError("Workspace content exceeds the write limit")

        target = self._resolve_write_target(relative_path)
        previous_content = self._read_optional_text(target)
        previous_sha256 = self._sha256(previous_content) if previous_content is not None else None
        proposed_sha256 = hashlib.sha256(encoded_content).hexdigest()
        relative = target.relative_to(self.root).as_posix()
        before_lines = previous_content.splitlines(keepends=True) if previous_content else []
        after_lines = content.splitlines(keepends=True) if content else []
        diff = "".join(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"a/{relative}" if previous_content is not None else "/dev/null",
                tofile=f"b/{relative}",
            )
        )
        return WriteProposal(
            workspace_root=self.root,
            path=relative,
            content=content,
            diff=diff,
            is_new=previous_content is None,
            previous_sha256=previous_sha256,
            proposed_sha256=proposed_sha256,
        )

    def list_files(self, pattern: str = "**/*", *, limit: int = 200) -> FileList:
        if not 1 <= limit <= MAX_LIST_LIMIT:
            raise ValueError(f"File listing limit must be between 1 and {MAX_LIST_LIMIT}")
        normalized_pattern = self._validate_pattern(pattern)
        paths: list[str] = []
        truncated = False
        candidates_scanned = 0

        try:
            candidates = sorted(self.root.glob(normalized_pattern))
            for candidate in candidates:
                if candidates_scanned >= self.max_files_scanned:
                    truncated = True
                    break
                candidates_scanned += 1
                try:
                    relative = candidate.relative_to(self.root)
                except ValueError:
                    continue
                if self._is_excluded(relative):
                    continue
                try:
                    resolved = candidate.resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
                if (
                    not self._is_inside_root(resolved)
                    or not resolved.is_file()
                    or self._is_excluded_target(relative, resolved)
                ):
                    continue
                paths.append(relative.as_posix())
        except (OSError, ValueError) as exc:
            raise WorkspacePathError("File pattern could not be enumerated") from exc

        paths.sort()
        return FileList(paths=paths[:limit], truncated=truncated or len(paths) > limit)

    def search_text(
        self,
        query: str,
        *,
        pattern: str = "**/*",
        limit: int = 100,
        case_sensitive: bool = False,
    ) -> SearchResults:
        if not query:
            raise ValueError("Search query must not be empty")
        if len(query) > 4096:
            raise ValueError("Search query must be at most 4096 characters")
        if not 1 <= limit <= MAX_SEARCH_LIMIT:
            raise ValueError(f"Search result limit must be between 1 and {MAX_SEARCH_LIMIT}")

        normalized_pattern = self._validate_pattern(pattern)
        needle = query if case_sensitive else query.casefold()
        matches: list[FileMatch] = []
        files_scanned = 0
        files_skipped = 0
        truncated = False
        candidates_scanned = 0

        try:
            candidates = sorted(self.root.glob(normalized_pattern))
            for candidate in candidates:
                if candidates_scanned >= self.max_files_scanned:
                    truncated = True
                    break
                candidates_scanned += 1
                try:
                    relative = candidate.relative_to(self.root)
                except ValueError:
                    continue
                if self._is_excluded(relative):
                    continue
                try:
                    path = candidate.resolve(strict=True)
                    if (
                        not self._is_inside_root(path)
                        or not path.is_file()
                        or self._is_excluded_target(relative, path)
                    ):
                        continue
                    if path.stat().st_size > self.max_file_bytes:
                        files_skipped += 1
                        continue
                    with path.open("rb") as file:
                        content = file.read(self.max_file_bytes + 1)
                    if len(content) > self.max_file_bytes:
                        files_skipped += 1
                        continue
                    text = content.decode("utf-8")
                except (OSError, RuntimeError, UnicodeDecodeError):
                    files_skipped += 1
                    continue

                if "\x00" in text:
                    files_skipped += 1
                    continue
                files_scanned += 1
                for line_number, line in enumerate(text.splitlines(), start=1):
                    haystack = line if case_sensitive else line.casefold()
                    if needle not in haystack:
                        continue
                    if len(matches) >= limit:
                        truncated = True
                        break
                    matches.append(
                        FileMatch(
                            path=relative.as_posix(),
                            line_number=line_number,
                            line=line[:2000],
                            line_truncated=len(line) > 2000,
                        )
                    )
                if truncated:
                    break
        except (OSError, ValueError) as exc:
            raise WorkspacePathError("File pattern could not be searched") from exc

        return SearchResults(
            matches=matches,
            files_scanned=files_scanned,
            files_skipped=files_skipped,
            truncated=truncated,
        )

    def discover_context(self, *, file_limit: int = 200) -> WorkspaceContext:
        instructions_path = self.root / "FORGE.md"
        instructions: str | None = None
        if instructions_path.exists():
            instructions = self.read_text("FORGE.md")
        return WorkspaceContext(
            root=self.root,
            instructions=instructions,
            files=self.list_files(limit=file_limit),
        )

    def _resolve_file(self, relative_path: str | Path) -> Path:
        path = self._validate_relative_path(relative_path)
        relative = path

        candidate = self.root / path
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorkspacePathError("Workspace file does not exist or cannot be resolved") from exc
        if not self._is_inside_root(resolved) or self._is_excluded_target(relative, resolved):
            raise WorkspacePathError("Workspace path is outside the allowed readable files")
        if not resolved.is_file():
            raise WorkspacePathError("Workspace path is not a file")
        return resolved

    def _resolve_write_target(self, relative_path: str | Path) -> Path:
        path = self._validate_relative_path(relative_path)
        relative = path
        candidate = self.root / path

        current = self.root
        for part in path.parts:
            current = current / part
            if current.is_symlink():
                raise WorkspacePathError("Workspace writes cannot follow symbolic links")

        if self._is_excluded(relative):
            raise WorkspacePathError("Workspace path is outside the allowed writable files")
        try:
            parent = candidate.parent.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorkspacePathError("Workspace parent directory does not exist") from exc
        if (
            not self._is_inside_root(parent)
            or not parent.is_dir()
            or self._is_excluded_target(relative.parent, parent)
        ):
            raise WorkspacePathError("Workspace path is outside the allowed writable files")

        target = parent / candidate.name
        if target.is_symlink():
            raise WorkspacePathError("Workspace writes cannot follow symbolic links")
        try:
            resolved = target.resolve(strict=target.exists())
        except (OSError, RuntimeError) as exc:
            raise WorkspacePathError("Workspace file cannot be resolved") from exc
        if (
            not self._is_inside_root(resolved)
            or self._is_excluded_target(relative, resolved)
        ):
            raise WorkspacePathError("Workspace path is outside the allowed writable files")
        if target.exists() and not resolved.is_file():
            raise WorkspacePathError("Workspace path is not a regular file")
        return resolved

    def _read_optional_text(self, path: Path) -> str | None:
        if not path.exists():
            return None
        if path.stat().st_size > self.max_file_bytes:
            raise WorkspaceFileTooLargeError("Workspace file exceeds the read limit")
        try:
            with path.open("rb") as file:
                content = file.read(self.max_file_bytes + 1)
        except OSError as exc:
            raise WorkspacePathError("Workspace file cannot be accessed") from exc
        if len(content) > self.max_file_bytes:
            raise WorkspaceFileTooLargeError("Workspace file exceeds the read limit")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceEncodingError("Workspace file is not valid UTF-8 text") from exc
        if "\x00" in text:
            raise WorkspaceEncodingError("Workspace file is not valid UTF-8 text")
        return text

    @staticmethod
    def _sha256(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _is_inside_root(self, path: Path) -> bool:
        try:
            path.relative_to(self.root)
            return True
        except ValueError:
            return False

    def _is_excluded_target(self, relative: Path, resolved: Path) -> bool:
        if self._is_excluded(relative):
            return True
        try:
            resolved_relative = resolved.relative_to(self.root)
        except ValueError:
            return True
        return self._is_excluded(resolved_relative)

    @staticmethod
    def _validate_pattern(pattern: str) -> str:
        windows_pattern = PureWindowsPath(pattern)
        path = Path(pattern)
        if (
            not pattern
            or "\x00" in pattern
            or path.is_absolute()
            or windows_pattern.is_absolute()
            or windows_pattern.drive
            or any(part == ".." for part in path.parts)
        ):
            raise WorkspacePathError("File pattern must be relative to the workspace")
        return pattern

    @staticmethod
    def _validate_relative_path(relative_path: str | Path) -> Path:
        path_text = str(relative_path)
        windows_path = PureWindowsPath(path_text)
        path = Path(path_text)
        if (
            not path_text
            or "\x00" in path_text
            or path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or any(part == ".." for part in path.parts)
        ):
            raise WorkspacePathError("Workspace path must be a relative path inside the workspace")
        return path

    @staticmethod
    def _is_excluded(relative: Path) -> bool:
        parts = [part.casefold() for part in relative.parts]
        if any(part in _IGNORED_DIRECTORIES or part in _SENSITIVE_DIRECTORIES for part in parts):
            return True

        filename = parts[-1] if parts else ""
        if filename.startswith(".env"):
            return not any(filename.endswith(suffix) for suffix in _SAFE_ENV_SUFFIXES)
        if filename in _SENSITIVE_FILENAMES:
            return True
        tokens = re.split(r"[^a-z0-9]+", filename)
        if any(token in {"credential", "credentials", "secret", "secrets"} for token in tokens):
            return True
        return Path(filename).suffix.casefold() in _SENSITIVE_SUFFIXES
