import hashlib
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path, PureWindowsPath

from forge.context.workspace import (
    Workspace,
    WorkspacePathError,
    WorkspacePermissionDeniedError,
)

logger = logging.getLogger("forge.checkpoint")

TASK_ID_REGEX = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_TASK_ID_LENGTH = 128


@dataclass
class FileSnapshot:
    path: str
    existed: bool
    content: str | None = None
    pre_sha256: str | None = None
    post_sha256: str | None = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class TaskCheckpoint:
    task_id: str
    created_at: float = field(default_factory=time.time)
    snapshots: dict[str, FileSnapshot] = field(default_factory=dict)


@dataclass
class RollbackResult:
    task_id: str
    restored: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


class CheckpointManager:
    """Records pre-change file states and manages safe rollbacks without clobbering user edits."""

    def __init__(self, workspace_root: Path, *, workspace: Workspace | None = None) -> None:
        self.workspace_root = workspace_root.resolve()
        self.checkpoints_dir = (self.workspace_root / ".forge" / "checkpoints").resolve()
        self._workspace = workspace or Workspace(self.workspace_root)

    def _validate_task_id(self, task_id: str) -> str:
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("Invalid task ID: must be a non-empty string")
        if not TASK_ID_REGEX.fullmatch(task_id) or len(task_id) > MAX_TASK_ID_LENGTH:
            raise ValueError(f"Invalid task ID format: {task_id!r}")
        return task_id

    def _get_checkpoint_path(self, task_id: str) -> Path:
        valid_id = self._validate_task_id(task_id)
        path = (self.checkpoints_dir / f"{valid_id}.json").resolve()
        try:
            path.relative_to(self.checkpoints_dir)
        except ValueError:
            raise ValueError(f"Task ID resolves outside checkpoints directory: {task_id!r}")
        return path

    def _validate_safe_path(self, rel_path: str) -> Path:
        if not isinstance(rel_path, str) or not rel_path or "\x00" in rel_path:
            raise WorkspacePathError("Path must be a non-empty string without null bytes")

        p = Path(rel_path)
        p_win = PureWindowsPath(rel_path)
        if (
            p.is_absolute()
            or p_win.is_absolute()
            or p_win.drive
            or any(part == ".." for part in p.parts)
        ):
            raise WorkspacePathError(f"Traversal or absolute path rejected: {rel_path}")

        # Check canonical workspace root
        target = (self.workspace_root / p).resolve()
        try:
            rel = target.relative_to(self.workspace_root)
            if not rel.parts:
                raise WorkspacePathError("Target cannot be workspace root itself")
        except ValueError:
            raise WorkspacePathError(f"Path escapes workspace root: {rel_path}")

        # Reject symlinks in target and any existing ancestor components up to workspace root
        curr = self.workspace_root / p
        while curr != self.workspace_root and curr != curr.parent:
            if curr.is_symlink() or os.path.islink(curr):
                raise WorkspacePathError(f"Symlink rejected in path: {rel_path}")
            curr = curr.parent

        # Prevent access to files excluded by workspace security rules
        if self._workspace._is_excluded(p) or self._workspace._is_excluded(rel):
            raise WorkspacePermissionDeniedError(
                f"File is excluded by workspace security rules: {rel_path}"
            )

        return self.workspace_root / p

    def record_pre_change(
        self,
        task_id: str,
        relative_path: str,
        current_content: str | None,
    ) -> None:
        """Record pre-change snapshot if not already recorded for this task."""
        self._validate_task_id(task_id)
        self._validate_safe_path(relative_path)
        checkpoint = self.get_checkpoint(task_id) or TaskCheckpoint(task_id=task_id)
        if relative_path in checkpoint.snapshots:
            return  # Preserve original pre-change state before first task edit

        pre_sha = (
            hashlib.sha256(current_content.encode("utf-8")).hexdigest()
            if current_content is not None
            else None
        )
        snapshot = FileSnapshot(
            path=relative_path,
            existed=current_content is not None,
            content=current_content,
            pre_sha256=pre_sha,
        )
        checkpoint.snapshots[relative_path] = snapshot
        self._save_checkpoint(checkpoint)

    def record_post_change(
        self,
        task_id: str,
        relative_path: str,
        post_content: str | None = None,
        *,
        post_sha256: str | None = None,
    ) -> None:
        self._validate_task_id(task_id)
        self._validate_safe_path(relative_path)
        checkpoint = self.get_checkpoint(task_id)
        if checkpoint and relative_path in checkpoint.snapshots:
            if post_sha256 is not None:
                post_sha = post_sha256
            elif post_content is not None:
                post_sha = hashlib.sha256(post_content.encode("utf-8")).hexdigest()
            else:
                post_sha = None
            checkpoint.snapshots[relative_path].post_sha256 = post_sha
            self._save_checkpoint(checkpoint)

    def get_checkpoint(self, task_id: str) -> TaskCheckpoint | None:
        path = self._get_checkpoint_path(task_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("task_id") != task_id:
                logger.warning("Checkpoint %s has invalid data or mismatched task_id", path)
                return None
            raw_snapshots = data.get("snapshots")
            if not isinstance(raw_snapshots, dict):
                logger.warning("Checkpoint %s has invalid snapshots map", path)
                return None
            snapshots = {}
            for p, s_data in raw_snapshots.items():
                if isinstance(s_data, dict):
                    snapshots[p] = FileSnapshot(**s_data)
            return TaskCheckpoint(
                task_id=data["task_id"],
                created_at=float(data.get("created_at", 0.0)),
                snapshots=snapshots,
            )
        except Exception as exc:
            logger.warning("Could not read checkpoint %s: %s", path, exc)
            return None

    def list_checkpoints(self) -> list[TaskCheckpoint]:
        if not self.checkpoints_dir.is_dir():
            return []
        checkpoints: list[TaskCheckpoint] = []
        for file in sorted(self.checkpoints_dir.glob("*.json")):
            try:
                cp = self.get_checkpoint(file.stem)
                if cp:
                    checkpoints.append(cp)
            except ValueError:
                continue
        checkpoints.sort(key=lambda c: c.created_at, reverse=True)
        return checkpoints

    def rollback(self, task_id: str) -> RollbackResult:
        try:
            checkpoint = self.get_checkpoint(task_id)
        except ValueError:
            return RollbackResult(task_id=task_id)

        if not checkpoint:
            return RollbackResult(task_id=task_id)

        restored: list[str] = []
        conflicts: list[str] = []
        skipped: list[str] = []

        for rel_path, snapshot in checkpoint.snapshots.items():
            # 1. Validate path against canonical root, reject traversal, symlinks, and excluded files
            try:
                target = self._validate_safe_path(rel_path)
            except Exception as exc:
                logger.warning("Rejected unsafe path in checkpoint rollback: %s (%s)", rel_path, exc)
                conflicts.append(rel_path)
                continue

            # 2. Validate snapshot data integrity
            if snapshot.path != rel_path:
                logger.warning("Tampered snapshot: path mismatch %s != %s", snapshot.path, rel_path)
                conflicts.append(rel_path)
                continue

            if snapshot.existed:
                if snapshot.content is None:
                    logger.warning("Tampered snapshot: existed is True but content is None for %s", rel_path)
                    conflicts.append(rel_path)
                    continue
                if snapshot.pre_sha256 is not None:
                    calc_pre = hashlib.sha256(snapshot.content.encode("utf-8")).hexdigest()
                    if calc_pre != snapshot.pre_sha256:
                        logger.warning("Tampered snapshot: pre_sha256 mismatch for %s", rel_path)
                        conflicts.append(rel_path)
                        continue
            else:
                if snapshot.content is not None:
                    logger.warning("Tampered snapshot: existed is False but content is not None for %s", rel_path)
                    conflicts.append(rel_path)
                    continue

            # 3. Safe restoration without clobbering user edits
            if snapshot.existed:
                if not target.exists():
                    # The file existed originally, but user deleted it externally
                    conflicts.append(rel_path)
                    continue
                if target.is_dir() or target.is_symlink() or os.path.islink(target):
                    conflicts.append(rel_path)
                    continue

                try:
                    current_content = target.read_text(encoding="utf-8")
                except Exception:
                    conflicts.append(rel_path)
                    continue

                current_sha = hashlib.sha256(current_content.encode("utf-8")).hexdigest()

                if snapshot.post_sha256 is not None:
                    if current_sha != snapshot.post_sha256:
                        # User modified the file after Forge edited it
                        conflicts.append(rel_path)
                        continue
                else:
                    if current_sha == snapshot.pre_sha256:
                        skipped.append(rel_path)
                        continue
                    else:
                        conflicts.append(rel_path)
                        continue

                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(snapshot.content, encoding="utf-8")
                    restored.append(rel_path)
                except Exception as exc:
                    logger.warning("Failed restoring %s: %s", rel_path, exc)
                    conflicts.append(rel_path)
            else:
                # File was newly created by Forge
                if not target.exists():
                    skipped.append(rel_path)
                    continue
                if target.is_dir() or target.is_symlink() or os.path.islink(target):
                    conflicts.append(rel_path)
                    continue

                try:
                    current_content = target.read_text(encoding="utf-8")
                except Exception:
                    conflicts.append(rel_path)
                    continue

                current_sha = hashlib.sha256(current_content.encode("utf-8")).hexdigest()

                if snapshot.post_sha256 is not None:
                    if current_sha != snapshot.post_sha256:
                        # User modified the new file after Forge created it
                        conflicts.append(rel_path)
                        continue
                else:
                    conflicts.append(rel_path)
                    continue

                try:
                    target.unlink()
                    restored.append(rel_path)
                except Exception as exc:
                    logger.warning("Failed unlinking %s: %s", rel_path, exc)
                    conflicts.append(rel_path)

        return RollbackResult(
            task_id=task_id,
            restored=restored,
            conflicts=conflicts,
            skipped=skipped,
        )

    def _save_checkpoint(self, checkpoint: TaskCheckpoint) -> None:
        self._validate_task_id(checkpoint.task_id)
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        path = self._get_checkpoint_path(checkpoint.task_id)
        data = {
            "task_id": checkpoint.task_id,
            "created_at": checkpoint.created_at,
            "snapshots": {p: asdict(s) for p, s in checkpoint.snapshots.items()},
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
