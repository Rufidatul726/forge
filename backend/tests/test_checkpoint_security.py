import hashlib
import json
import os
from pathlib import Path

import pytest

from forge.agent import (
    AgentService,
    AgentTaskRequest,
    ExecutionPolicy,
    ExecutionPolicyConfig,
    load_execution_policy_config,
)
from forge.agent.dispatcher import ToolDispatcher
from forge.agent.registry import ToolExecutionContext, create_default_registry
from forge.agent.schemas import RunCommandArgs, WriteFileArgs
from forge.context import (
    CheckpointManager,
    FileSnapshot,
    TaskCheckpoint,
    Workspace,
    WorkspacePathError,
    WorkspacePermissionDeniedError,
)
from forge.execution import CommandRunner
from forge.providers.models import ChatMessage, GenerationResult, ToolCall


# ============================================================================
# 1. Checkpoint Security & Validation Tests
# ============================================================================


def test_checkpoint_invalid_task_ids(tmp_path: Path):
    mgr = CheckpointManager(tmp_path)

    invalid_ids = [
        "../../etc/passwd",
        "../task-1",
        "task/subtask",
        "task\\win",
        "task\x00null",
        "",
        "task;rm -rf",
        "task name with spaces",
        "a" * 200,  # exceeds max length
    ]

    for bad_id in invalid_ids:
        with pytest.raises(ValueError, match="Invalid task ID"):
            mgr._get_checkpoint_path(bad_id)

        with pytest.raises(ValueError, match="Invalid task ID"):
            mgr.record_pre_change(bad_id, "file.txt", "content")

        with pytest.raises(ValueError, match="Invalid task ID"):
            mgr.record_post_change(bad_id, "file.txt", "content")

        with pytest.raises(ValueError, match="Invalid task ID"):
            mgr.get_checkpoint(bad_id)

        res = mgr.rollback(bad_id)
        assert res.task_id == bad_id
        assert not res.restored
        assert not res.conflicts


def test_checkpoint_rejects_malicious_restored_paths(tmp_path: Path):
    outside_file = tmp_path.parent / "sensitive_outside.txt"
    outside_file.write_text("critical outside data\n", encoding="utf-8")

    mgr = CheckpointManager(tmp_path)
    task_id = "task-malicious-paths"

    # Construct checkpoint JSON directly to simulate tampered/malicious checkpoint data
    cp_dir = tmp_path / ".forge" / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)
    cp_file = cp_dir / f"{task_id}.json"

    malicious_snapshots = {
        # Path traversal
        "../../sensitive_outside.txt": {
            "path": "../../sensitive_outside.txt",
            "existed": True,
            "content": "overwritten outside\n",
            "pre_sha256": hashlib.sha256(b"critical outside data\n").hexdigest(),
            "post_sha256": hashlib.sha256(b"tampered\n").hexdigest(),
        },
        # Absolute path (POSIX style)
        "/etc/passwd": {
            "path": "/etc/passwd",
            "existed": True,
            "content": "fake root\n",
            "pre_sha256": hashlib.sha256(b"fake root\n").hexdigest(),
            "post_sha256": "abc",
        },
    }

    cp_file.write_text(
        json.dumps({
            "task_id": task_id,
            "created_at": 100.0,
            "snapshots": malicious_snapshots,
        }),
        encoding="utf-8",
    )

    res = mgr.rollback(task_id)
    assert "../../sensitive_outside.txt" in res.conflicts
    assert "/etc/passwd" in res.conflicts
    assert not res.restored

    # Ensure outside file was completely untouched
    assert outside_file.read_text(encoding="utf-8") == "critical outside data\n"


def test_checkpoint_rejects_symlinks(tmp_path: Path):
    real_target = tmp_path / "real_file.py"
    real_target.write_text("original content\n", encoding="utf-8")

    link_file = tmp_path / "symlink_file.py"
    try:
        link_file.symlink_to(real_target)
    except OSError:
        pytest.skip("Symlinks not permitted in this environment")

    mgr = CheckpointManager(tmp_path)

    # Directly recording pre-change on a symlink must be rejected
    with pytest.raises(WorkspacePathError, match="Symlink rejected"):
        mgr.record_pre_change("task-symlink", "symlink_file.py", "original content\n")

    # Tampered checkpoint containing a symlink must be rejected during rollback
    task_id = "task-symlink"
    cp_dir = tmp_path / ".forge" / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)
    (cp_dir / f"{task_id}.json").write_text(
        json.dumps({
            "task_id": task_id,
            "created_at": 100.0,
            "snapshots": {
                "symlink_file.py": {
                    "path": "symlink_file.py",
                    "existed": True,
                    "content": "reverted\n",
                    "pre_sha256": hashlib.sha256(b"reverted\n").hexdigest(),
                    "post_sha256": hashlib.sha256(b"original content\n").hexdigest(),
                }
            },
        }),
        encoding="utf-8",
    )

    res = mgr.rollback(task_id)
    assert "symlink_file.py" in res.conflicts
    assert not res.restored
    assert real_target.read_text(encoding="utf-8") == "original content\n"


def test_checkpoint_rejects_symlink_mocked(tmp_path: Path, monkeypatch):
    target = tmp_path / "mock_symlink.py"
    target.write_text("content\n", encoding="utf-8")

    mgr = CheckpointManager(tmp_path)

    # Monkeypatch is_symlink on Path to return True for mock_symlink.py
    orig_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "mock_symlink.py" or orig_is_symlink(self))

    with pytest.raises(WorkspacePathError, match="Symlink rejected"):
        mgr.record_pre_change("task-mock-symlink", "mock_symlink.py", "content\n")

    task_id = "task-mock-symlink"
    cp_dir = tmp_path / ".forge" / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)
    (cp_dir / f"{task_id}.json").write_text(
        json.dumps({
            "task_id": task_id,
            "created_at": 100.0,
            "snapshots": {
                "mock_symlink.py": {
                    "path": "mock_symlink.py",
                    "existed": True,
                    "content": "changed\n",
                    "pre_sha256": hashlib.sha256(b"changed\n").hexdigest(),
                    "post_sha256": hashlib.sha256(b"content\n").hexdigest(),
                }
            },
        }),
        encoding="utf-8",
    )
    res = mgr.rollback(task_id)
    assert "mock_symlink.py" in res.conflicts
    assert not res.restored


def test_checkpoint_prevents_rollback_of_excluded_files(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgres://secret\n", encoding="utf-8")

    mgr = CheckpointManager(tmp_path)

    # Direct recording on excluded files is rejected
    with pytest.raises(WorkspacePermissionDeniedError):
        mgr.record_pre_change("task-excluded", ".env", "secret")

    # Rollback of excluded file in checkpoint must be rejected
    task_id = "task-excluded"
    cp_dir = tmp_path / ".forge" / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)
    (cp_dir / f"{task_id}.json").write_text(
        json.dumps({
            "task_id": task_id,
            "created_at": 100.0,
            "snapshots": {
                ".env": {
                    "path": ".env",
                    "existed": True,
                    "content": "DATABASE_URL=postgres://tampered\n",
                    "pre_sha256": hashlib.sha256(b"DATABASE_URL=postgres://tampered\n").hexdigest(),
                    "post_sha256": hashlib.sha256(b"DATABASE_URL=postgres://secret\n").hexdigest(),
                }
            },
        }),
        encoding="utf-8",
    )

    res = mgr.rollback(task_id)
    assert ".env" in res.conflicts
    assert not res.restored
    assert env_file.read_text(encoding="utf-8") == "DATABASE_URL=postgres://secret\n"


def test_checkpoint_missing_files_handling(tmp_path: Path):
    mgr = CheckpointManager(tmp_path)
    task_id = "task-missing"

    # Case 1: Existed originally, but deleted by user externally before rollback
    target_existing = tmp_path / "existing.py"
    target_existing.write_text("original\n", encoding="utf-8")
    mgr.record_pre_change(task_id, "existing.py", "original\n")
    target_existing.write_text("modified by forge\n", encoding="utf-8")
    mgr.record_post_change(task_id, "existing.py", "modified by forge\n")

    # User externally deletes the file
    target_existing.unlink()

    # Case 2: Newly created by Forge, but deleted by user externally before rollback
    mgr.record_pre_change(task_id, "created.py", None)
    created_file = tmp_path / "created.py"
    created_file.write_text("new content\n", encoding="utf-8")
    mgr.record_post_change(task_id, "created.py", "new content\n")
    created_file.unlink()

    res = mgr.rollback(task_id)

    # Deleted existing file is a conflict (never clobber user's deletion)
    assert "existing.py" in res.conflicts
    assert not target_existing.exists()

    # Deleted created file is cleanly marked skipped (desired end state already achieved)
    assert "created.py" in res.skipped


def test_checkpoint_tampered_checkpoint_data(tmp_path: Path):
    target = tmp_path / "code.py"
    target.write_text("current code\n", encoding="utf-8")
    curr_sha = hashlib.sha256(b"current code\n").hexdigest()

    mgr = CheckpointManager(tmp_path)
    task_id = "task-tampered"
    cp_dir = tmp_path / ".forge" / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)

    # 1. Checkpoint JSON with tampered content (content does not match pre_sha256)
    (cp_dir / f"{task_id}.json").write_text(
        json.dumps({
            "task_id": task_id,
            "created_at": 100.0,
            "snapshots": {
                "code.py": {
                    "path": "code.py",
                    "existed": True,
                    "content": "injected malicious code\n",
                    "pre_sha256": hashlib.sha256(b"genuine pre-change code\n").hexdigest(),
                    "post_sha256": curr_sha,
                }
            },
        }),
        encoding="utf-8",
    )

    res = mgr.rollback(task_id)
    assert "code.py" in res.conflicts
    assert not res.restored
    assert target.read_text(encoding="utf-8") == "current code\n"

    # 2. Checkpoint JSON with mismatched task_id inside the payload
    task_id_mismatch = "task-mismatch"
    (cp_dir / f"{task_id_mismatch}.json").write_text(
        json.dumps({
            "task_id": "different-id",
            "created_at": 100.0,
            "snapshots": {},
        }),
        encoding="utf-8",
    )
    assert mgr.get_checkpoint(task_id_mismatch) is None

    # 3. Corrupted non-JSON file
    task_id_corrupt = "task-corrupt"
    (cp_dir / f"{task_id_corrupt}.json").write_text("{invalid json", encoding="utf-8")
    assert mgr.get_checkpoint(task_id_corrupt) is None


# ============================================================================
# 2. Checkpoint Tracking Correctness in Registry Tests
# ============================================================================


@pytest.mark.anyio
async def test_checkpoint_tracking_new_and_existing_files(tmp_path: Path):
    ws = Workspace(tmp_path)
    cmd = CommandRunner(tmp_path)
    mgr = CheckpointManager(tmp_path)
    reg = create_default_registry()
    task_id = "task-registry-test"

    ctx = ToolExecutionContext(
        workspace=ws,
        command_runner=cmd,
        policy_config=ExecutionPolicyConfig(policy=ExecutionPolicy.NORMAL),
        approve_write=lambda p: True,
        approve_command=lambda p: True,
        checkpoint_manager=mgr,
        task_id=task_id,
    )
    dispatcher = ToolDispatcher(reg, ctx)

    # 1. Write new file
    res1 = await dispatcher.dispatch(
        ToolCall(id="1", name="write_file", arguments={"path": "new.py", "content": "print('new')\n"})
    )
    assert res1.ok is True
    assert "Updated new.py" in res1.content

    cp = mgr.get_checkpoint(task_id)
    assert cp is not None
    assert "new.py" in cp.snapshots
    assert cp.snapshots["new.py"].existed is False
    assert cp.snapshots["new.py"].content is None
    assert cp.snapshots["new.py"].pre_sha256 is None
    assert cp.snapshots["new.py"].post_sha256 == hashlib.sha256(b"print('new')\n").hexdigest()

    # 2. Update existing file
    res2 = await dispatcher.dispatch(
        ToolCall(id="2", name="write_file", arguments={"path": "new.py", "content": "print('updated')\n"})
    )
    assert res2.ok is True

    cp2 = mgr.get_checkpoint(task_id)
    # The pre-change state is preserved from the very first modification (existed=False)
    assert cp2.snapshots["new.py"].existed is False
    assert cp2.snapshots["new.py"].post_sha256 == hashlib.sha256(b"print('updated')\n").hexdigest()

    # 3. Rollback restores to pre-task state (deletes the newly created file)
    roll_res = mgr.rollback(task_id)
    assert "new.py" in roll_res.restored
    assert not (tmp_path / "new.py").exists()


@pytest.mark.anyio
async def test_checkpoint_unchanged_file_does_not_create_snapshot(tmp_path: Path):
    (tmp_path / "same.py").write_bytes(b"hello world\n")

    ws = Workspace(tmp_path)
    cmd = CommandRunner(tmp_path)
    mgr = CheckpointManager(tmp_path)
    reg = create_default_registry()
    task_id = "task-unchanged"

    ctx = ToolExecutionContext(
        workspace=ws,
        command_runner=cmd,
        policy_config=ExecutionPolicyConfig(policy=ExecutionPolicy.NORMAL),
        approve_write=lambda p: True,
        approve_command=lambda p: True,
        checkpoint_manager=mgr,
        task_id=task_id,
    )
    dispatcher = ToolDispatcher(reg, ctx)

    # Writing identical content
    res = await dispatcher.dispatch(
        ToolCall(id="1", name="write_file", arguments={"path": "same.py", "content": "hello world\n"})
    )
    assert res.ok is True
    assert "No change needed" in res.content

    # No checkpoint created
    assert mgr.get_checkpoint(task_id) is None


@pytest.mark.anyio
async def test_checkpoint_rejected_write_does_not_create_snapshot(tmp_path: Path):
    (tmp_path / "guarded.py").write_bytes(b"protected content\n")

    ws = Workspace(tmp_path)
    cmd = CommandRunner(tmp_path)
    mgr = CheckpointManager(tmp_path)
    reg = create_default_registry()
    task_id = "task-rejected"

    ctx = ToolExecutionContext(
        workspace=ws,
        command_runner=cmd,
        policy_config=ExecutionPolicyConfig(policy=ExecutionPolicy.NORMAL),
        approve_write=lambda p: False,  # REJECT WRITE
        approve_command=lambda p: True,
        checkpoint_manager=mgr,
        task_id=task_id,
    )
    dispatcher = ToolDispatcher(reg, ctx)

    res = await dispatcher.dispatch(
        ToolCall(id="1", name="write_file", arguments={"path": "guarded.py", "content": "mutated\n"})
    )
    assert res.ok is False
    assert (tmp_path / "guarded.py").read_text(encoding="utf-8") == "protected content\n"

    # Rejected write must not record any checkpoint
    assert mgr.get_checkpoint(task_id) is None


@pytest.mark.anyio
async def test_checkpoint_partial_failure(tmp_path: Path):
    (tmp_path / "file_a.py").write_bytes(b"content A\n")

    ws = Workspace(tmp_path)
    cmd = CommandRunner(tmp_path)
    mgr = CheckpointManager(tmp_path)
    reg = create_default_registry()
    task_id = "task-partial"

    # Allow write for file_a, reject for file_b
    def approve_selective(proposal):
        return proposal.path == "file_a.py"

    ctx = ToolExecutionContext(
        workspace=ws,
        command_runner=cmd,
        policy_config=ExecutionPolicyConfig(policy=ExecutionPolicy.NORMAL),
        approve_write=approve_selective,
        approve_command=lambda p: True,
        checkpoint_manager=mgr,
        task_id=task_id,
    )
    dispatcher = ToolDispatcher(reg, ctx)

    # Succeeded write
    res_a = await dispatcher.dispatch(
        ToolCall(id="1", name="write_file", arguments={"path": "file_a.py", "content": "updated A\n"})
    )
    assert res_a.ok is True

    # Failed/rejected write
    res_b = await dispatcher.dispatch(
        ToolCall(id="2", name="write_file", arguments={"path": "file_b.py", "content": "updated B\n"})
    )
    assert res_b.ok is False

    cp = mgr.get_checkpoint(task_id)
    assert cp is not None
    assert "file_a.py" in cp.snapshots
    assert "file_b.py" not in cp.snapshots

    # Rollback restores file_a and does not affect file_b
    roll_res = mgr.rollback(task_id)
    assert "file_a.py" in roll_res.restored
    assert "file_b.py" not in roll_res.conflicts
    assert (tmp_path / "file_a.py").read_text(encoding="utf-8") == "content A\n"


# ============================================================================
# 3. Execution Policy Combination Tests
# ============================================================================


def test_execution_policy_all_combinations(tmp_path: Path):
    global_cfg = tmp_path / "global.json"
    project_cfg = tmp_path / "project.json"

    # Case 1: Plan mode cannot mutate files or execute commands even if tools are allowed
    plan_cfg = ExecutionPolicyConfig(
        policy=ExecutionPolicy.PLAN,
        allowed_autonomous_tools={"write_file", "run_command", "read_file"},
    )
    # Read-only tools allowed
    perm_read, _ = plan_cfg.is_permitted("read_file", read_only=True)
    assert perm_read is True

    # Mutating tools strictly denied
    perm_write, reason_write = plan_cfg.is_permitted("write_file", read_only=False)
    assert perm_write is False
    assert "plan mode" in reason_write.lower()

    perm_cmd, reason_cmd = plan_cfg.is_permitted("run_command", read_only=False)
    assert perm_cmd is False
    assert "plan mode" in reason_cmd.lower()

    # Case 2: Autonomous mode without permissions blocks mutating tools
    auto_empty = ExecutionPolicyConfig(
        policy=ExecutionPolicy.AUTONOMOUS,
        allowed_autonomous_tools=set(),
    )
    perm_read_auto, _ = auto_empty.is_permitted("read_file", read_only=True)
    assert perm_read_auto is True
    perm_write_auto, reason_auto = auto_empty.is_permitted("write_file", read_only=False)
    assert perm_write_auto is False
    assert "explicit permission" in reason_auto.lower()

    # Case 3: Autonomous mode with explicit permission permits allowed tool without approval prompt
    auto_allowed = ExecutionPolicyConfig(
        policy=ExecutionPolicy.AUTONOMOUS,
        allowed_autonomous_tools={"write_file"},
    )
    perm_w, _ = auto_allowed.is_permitted("write_file", read_only=False)
    assert perm_w is True
    assert auto_allowed.requires_approval("write_file", read_only=False) is False

    perm_c, _ = auto_allowed.is_permitted("run_command", read_only=False)
    assert perm_c is False

    # Case 4: Normal mode permits tools but requires approval for mutating actions
    normal_cfg = ExecutionPolicyConfig(policy=ExecutionPolicy.NORMAL)
    perm_nw, _ = normal_cfg.is_permitted("write_file", read_only=False)
    assert perm_nw is True
    assert normal_cfg.requires_approval("write_file", read_only=False) is True
    assert normal_cfg.requires_approval("read_file", read_only=True) is False

    # Case 5: Hierarchy - Project overrides Global, CLI overrides Project
    global_cfg.write_text(
        json.dumps({
            "policy": "normal",
            "allowed_autonomous_tools": ["tool1"],
        }),
        encoding="utf-8",
    )
    project_cfg.write_text(
        json.dumps({
            "policy": "autonomous",
            "allowed_autonomous_tools": ["tool2"],
        }),
        encoding="utf-8",
    )

    loaded = load_execution_policy_config(
        global_config_path=global_cfg,
        project_config_path=project_cfg,
    )
    assert loaded.policy == ExecutionPolicy.AUTONOMOUS
    assert "tool1" in loaded.allowed_autonomous_tools
    assert "tool2" in loaded.allowed_autonomous_tools

    # CLI policy override takes highest precedence
    cli_overridden = load_execution_policy_config(
        global_config_path=global_cfg,
        project_config_path=project_cfg,
        cli_policy=ExecutionPolicy.PLAN,
        cli_allowed_tools=["tool3"],
    )
    assert cli_overridden.policy == ExecutionPolicy.PLAN
    assert "tool3" in cli_overridden.allowed_autonomous_tools
    # Still forbidden to write in plan mode
    perm_plan, _ = cli_overridden.is_permitted("write_file", read_only=False)
    assert perm_plan is False


@pytest.mark.anyio
async def test_plan_mode_dispatcher_blocks_mutations_and_commands(tmp_path: Path):
    target = tmp_path / "test.txt"
    target.write_text("untouched\n", encoding="utf-8")

    ws = Workspace(tmp_path)
    cmd = CommandRunner(tmp_path)
    reg = create_default_registry()

    # Plan mode configured with all autonomous tools allowlisted
    ctx = ToolExecutionContext(
        workspace=ws,
        command_runner=cmd,
        policy_config=ExecutionPolicyConfig(
            policy=ExecutionPolicy.PLAN,
            allowed_autonomous_tools={"write_file", "run_command"},
        ),
        approve_write=lambda p: True,
        approve_command=lambda p: True,
    )
    dispatcher = ToolDispatcher(reg, ctx)

    # 1. read_file permitted
    res_read = await dispatcher.dispatch(
        ToolCall(id="1", name="read_file", arguments={"path": "test.txt"})
    )
    assert res_read.ok is True
    assert "untouched" in res_read.content

    # 2. write_file blocked
    res_write = await dispatcher.dispatch(
        ToolCall(id="2", name="write_file", arguments={"path": "test.txt", "content": "mutated\n"})
    )
    assert res_write.ok is False
    assert "not permitted in plan mode" in res_write.content
    assert target.read_text(encoding="utf-8") == "untouched\n"

    # 3. run_command blocked
    res_cmd = await dispatcher.dispatch(
        ToolCall(id="3", name="run_command", arguments={"argv": ["python", "--version"]})
    )
    assert res_cmd.ok is False
    assert "not permitted in plan mode" in res_cmd.content


def test_checkpoint_payload_checksum_and_version(tmp_path: Path):
    mgr = CheckpointManager(tmp_path)
    task_id = "task-checksum-version"
    file_path = tmp_path / "hello.py"
    file_path.write_text("orig\n", encoding="utf-8")

    mgr.record_pre_change(task_id, "hello.py", "orig\n")
    mgr.record_post_change(task_id, "hello.py", "new\n")

    cp = mgr.get_checkpoint(task_id)
    assert cp is not None
    assert cp.version == 1
    assert cp.payload_checksum is not None

    # Checkpoint JSON on disk contains canonical payload_checksum and version
    cp_file = tmp_path / ".forge" / "checkpoints" / f"{task_id}.json"
    data = json.loads(cp_file.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["payload_checksum"] == cp.payload_checksum

    # Tamper with the checkpoint payload data without updating payload_checksum
    data["created_at"] = 999999.0
    cp_file.write_text(json.dumps(data), encoding="utf-8")

    # get_checkpoint detects the corrupted/tampered payload checksum and rejects it
    assert mgr.get_checkpoint(task_id) is None

    # Rollback safely handles tampered checkpoint without error or corrupting workspace
    res = mgr.rollback(task_id)
    assert not res.restored

