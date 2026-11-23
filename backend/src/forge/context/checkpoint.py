import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger("forge.checkpoint")


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

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self.checkpoints_dir = self.workspace_root / ".forge" / "checkpoints"

    def _get_checkpoint_path(self, task_id: str) -> Path:
        return self.checkpoints_dir / f"{task_id}.json"

    def record_pre_change(
        self,
        task_id: str,
        relative_path: str,
        current_content: str | None,
    ) -> None:
        """Record pre-change snapshot if not already recorded for this task."""
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
        post_content: str,
    ) -> None:
        checkpoint = self.get_checkpoint(task_id)
        if checkpoint and relative_path in checkpoint.snapshots:
            post_sha = hashlib.sha256(post_content.encode("utf-8")).hexdigest()
            checkpoint.snapshots[relative_path].post_sha256 = post_sha
            self._save_checkpoint(checkpoint)

    def get_checkpoint(self, task_id: str) -> TaskCheckpoint | None:
        path = self._get_checkpoint_path(task_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            snapshots = {
                p: FileSnapshot(**s_data)
                for p, s_data in data.get("snapshots", {}).items()
            }
            return TaskCheckpoint(
                task_id=data["task_id"],
                created_at=data.get("created_at", 0.0),
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
            cp = self.get_checkpoint(file.stem)
            if cp:
                checkpoints.append(cp)
        checkpoints.sort(key=lambda c: c.created_at, reverse=True)
        return checkpoints

    def rollback(self, task_id: str) -> RollbackResult:
        checkpoint = self.get_checkpoint(task_id)
        if not checkpoint:
            return RollbackResult(task_id=task_id)

        restored: list[str] = []
        conflicts: list[str] = []
        skipped: list[str] = []

        for rel_path, snapshot in checkpoint.snapshots.items():
            target = self.workspace_root / rel_path
            current_content: str | None = None
            if target.is_file():
                try:
                    current_content = target.read_text(encoding="utf-8")
                except Exception:
                    conflicts.append(rel_path)
                    continue

            current_sha = (
                hashlib.sha256(current_content.encode("utf-8")).hexdigest()
                if current_content is not None
                else None
            )

            # Check if user made unrelated changes:
            # If current content does not match the post-change content written by Forge,
            # someone modified it afterwards. Never overwrite unrelated changes!
            if snapshot.post_sha256 is not None and current_sha != snapshot.post_sha256:
                conflicts.append(rel_path)
                continue

            # Safe to restore
            try:
                if not snapshot.existed:
                    if target.is_file():
                        target.unlink()
                        restored.append(rel_path)
                    else:
                        skipped.append(rel_path)
                else:
                    if snapshot.content is not None:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(snapshot.content, encoding="utf-8")
                        restored.append(rel_path)
                    else:
                        skipped.append(rel_path)
            except Exception as exc:
                logger.warning("Failed restoring %s: %s", rel_path, exc)
                conflicts.append(rel_path)

        return RollbackResult(
            task_id=task_id,
            restored=restored,
            conflicts=conflicts,
            skipped=skipped,
        )

    def _save_checkpoint(self, checkpoint: TaskCheckpoint) -> None:
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        path = self._get_checkpoint_path(checkpoint.task_id)
        data = {
            "task_id": checkpoint.task_id,
            "created_at": checkpoint.created_at,
            "snapshots": {p: asdict(s) for p, s in checkpoint.snapshots.items()},
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
