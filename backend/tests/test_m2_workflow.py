import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from forge.agent import (
    AgentService,
    AgentTaskRequest,
    ExecutionPolicy,
    ExecutionPolicyConfig,
    load_execution_policy_config,
)
from forge.context import (
    CheckpointManager,
    Workspace,
    WorkspaceError,
)
from forge.providers.models import (
    ChatMessage,
    GenerationResult,
    ToolCall,
)


def run(coro):
    return asyncio.run(coro)


# 1. Nested instructions tests
def test_nested_instructions_discovery(tmp_path: Path):
    (tmp_path / "FORGE.md").write_text("Root forge instructions", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("Root agent instructions", encoding="utf-8")
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "FORGE.md").write_text("Backend rules", encoding="utf-8")
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "AGENTS.md").write_text("Frontend rules", encoding="utf-8")

    ws = Workspace(tmp_path)
    instructions = ws.discover_instructions()
    assert "FORGE.md" in instructions
    assert "AGENTS.md" in instructions
    assert "backend/FORGE.md" in instructions
    assert "frontend/AGENTS.md" in instructions

    ctx = ws.discover_context()
    assert "Root forge instructions" in ctx.instructions
    assert "Root agent instructions" in ctx.instructions
    assert "Backend rules" in ctx.instructions
    assert "Frontend rules" in ctx.instructions


# 2. Gitignore respects ignored patterns
def test_gitignore_pattern_matching(tmp_path: Path):
    (tmp_path / ".gitignore").write_text("*.log\nsecret_dir/\nlocal_config.json\n", encoding="utf-8")
    (tmp_path / "app.log").write_text("log data", encoding="utf-8")
    (tmp_path / "local_config.json").write_text("{\"key\": 1}", encoding="utf-8")
    (tmp_path / "secret_dir").mkdir()
    (tmp_path / "secret_dir" / "data.txt").write_text("private", encoding="utf-8")
    (tmp_path / "main.py").write_text("print(1)", encoding="utf-8")

    ws = Workspace(tmp_path)
    files = ws.list_files()
    assert "main.py" in files.paths
    assert "app.log" not in files.paths
    assert "local_config.json" not in files.paths
    assert "secret_dir/data.txt" not in files.paths

    with pytest.raises(WorkspaceError):
        ws.read_text("app.log")


# 3. Symbol search tests
def test_symbol_search_python_and_ts(tmp_path: Path):
    py_code = """
class Calculator:
    def add(self, a, b):
        return a + b

async def fetch_data():
    pass
"""
    ts_code = """
export interface User {
    id: string;
}

export type ID = string | number;

export class UserService {
    getUser() {}
}

export function helper() {}
"""
    (tmp_path / "calc.py").write_text(py_code, encoding="utf-8")
    (tmp_path / "user.ts").write_text(ts_code, encoding="utf-8")

    ws = Workspace(tmp_path)

    # Search class
    res_class = ws.search_symbols("Calculator")
    assert len(res_class.matches) == 1
    assert res_class.matches[0].name == "Calculator"
    assert res_class.matches[0].kind == "class"
    assert res_class.matches[0].path == "calc.py"

    # Search async function
    res_func = ws.search_symbols("fetch_data")
    assert len(res_func.matches) == 1
    assert res_func.matches[0].name == "fetch_data"
    assert res_func.matches[0].kind == "function"

    # Search interface & type
    res_ts = ws.search_symbols("User")
    names = {m.name: m.kind for m in res_ts.matches}
    assert "User" in names
    assert names["User"] == "interface"
    assert "UserService" in names
    assert names["UserService"] == "class"


# 4. Token-budget-aware context selection
def test_token_budget_aware_context_selection(tmp_path: Path):
    (tmp_path / "FORGE.md").write_text("A" * 10_000, encoding="utf-8")
    for i in range(20):
        (tmp_path / f"file_{i}.txt").write_text("line\n", encoding="utf-8")
    (tmp_path / "target1.py").write_text("B" * 5000, encoding="utf-8")
    (tmp_path / "target2.py").write_text("C" * 5000, encoding="utf-8")

    class FakeProvider:
        async def generate(self, payload):
            return GenerationResult(model="test", content="ok", done=True)

    svc = AgentService(tmp_path, FakeProvider())
    context = svc._build_context(["target1.py", "target2.py"], max_token_budget=1000)
    # Total chars must be within budget (1000 tokens * 4 = 4000 chars)
    assert len(context) <= 4000
    assert "Project instructions:" in context
    assert "target1.py" in context
    assert "target2.py" in context


# 5. Checkpoint and rollback tests
def test_checkpoint_and_safe_rollback(tmp_path: Path):
    file1 = tmp_path / "hello.py"
    file1.write_text("print('version 1')\n", encoding="utf-8")

    mgr = CheckpointManager(tmp_path)
    task_id = "task-001"

    # 1. Record pre-change and post-change for modification
    mgr.record_pre_change(task_id, "hello.py", "print('version 1')\n")
    file1.write_text("print('version 2 by forge')\n", encoding="utf-8")
    mgr.record_post_change(task_id, "hello.py", "print('version 2 by forge')\n")

    # 2. Record pre-change and post-change for newly created file
    mgr.record_pre_change(task_id, "created.py", None)
    created = tmp_path / "created.py"
    created.write_text("created by forge\n", encoding="utf-8")
    mgr.record_post_change(task_id, "created.py", "created by forge\n")

    # Verify checkpoint stored
    cp = mgr.get_checkpoint(task_id)
    assert cp is not None
    assert "hello.py" in cp.snapshots
    assert cp.snapshots["hello.py"].existed is True
    assert "created.py" in cp.snapshots
    assert cp.snapshots["created.py"].existed is False

    # 3. Perform rollback
    res = mgr.rollback(task_id)
    assert "hello.py" in res.restored
    assert "created.py" in res.restored
    assert not res.conflicts

    assert file1.read_text(encoding="utf-8") == "print('version 1')\n"
    assert not created.exists()


def test_rollback_protects_user_edits_from_overwrite(tmp_path: Path):
    file1 = tmp_path / "hello.py"
    file1.write_text("original\n", encoding="utf-8")

    mgr = CheckpointManager(tmp_path)
    task_id = "task-conflict"

    mgr.record_pre_change(task_id, "hello.py", "original\n")
    file1.write_text("written by forge\n", encoding="utf-8")
    mgr.record_post_change(task_id, "hello.py", "written by forge\n")

    # User modifies file afterwards!
    file1.write_text("user unrelated edit\n", encoding="utf-8")

    # Attempt rollback
    res = mgr.rollback(task_id)
    assert "hello.py" in res.conflicts
    assert "hello.py" not in res.restored
    # User's edit was preserved!
    assert file1.read_text(encoding="utf-8") == "user unrelated edit\n"


# 6. Execution policy configuration loader
def test_execution_policy_config_hierarchical_loading(tmp_path: Path):
    global_cfg = tmp_path / "global_config.json"
    project_cfg = tmp_path / ".forge" / "config.json"
    project_cfg.parent.mkdir(parents=True)

    global_cfg.write_text(
        json.dumps({
            "policy": "normal",
            "allowed_autonomous_tools": ["read_file"],
        }),
        encoding="utf-8",
    )
    project_cfg.write_text(
        json.dumps({
            "policy": "autonomous",
            "allowed_autonomous_tools": ["search_text"],
        }),
        encoding="utf-8",
    )

    # Loads and merges
    cfg = load_execution_policy_config(
        workspace_root=tmp_path,
        global_config_path=global_cfg,
    )
    assert cfg.policy == ExecutionPolicy.AUTONOMOUS
    assert "read_file" in cfg.allowed_autonomous_tools
    assert "search_text" in cfg.allowed_autonomous_tools

    # CLI policy override
    cfg_cli = load_execution_policy_config(
        workspace_root=tmp_path,
        global_config_path=global_cfg,
        cli_policy=ExecutionPolicy.PLAN,
    )
    assert cfg_cli.policy == ExecutionPolicy.PLAN
    # Plan mode strictly forbids mutations even if tools are in allowed set
    permitted, reason = cfg_cli.is_permitted("write_file", read_only=False)
    assert permitted is False
    assert "plan mode" in reason.lower()


# 7. Complete coding workflow end-to-end
def test_complete_coding_workflow_simulation(tmp_path: Path):
    (tmp_path / "math_lib.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (tmp_path / "test_math.py").write_text(
        "from math_lib import add\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )

    class WorkflowProvider:
        def __init__(self):
            self.turn = 0

        async def generate(self, payload):
            self.turn += 1
            if self.turn == 1:
                # 1. Repository exploration & search
                return GenerationResult(
                    model="test",
                    tool_calls=[
                        ToolCall(id="tc1", name="search_symbols", arguments={"query": "add"}),
                        ToolCall(id="tc2", name="read_file", arguments={"path": "math_lib.py"}),
                    ],
                    done=True,
                )
            elif self.turn == 2:
                # 2. Planning and file modification
                return GenerationResult(
                    model="test",
                    tool_calls=[
                        ToolCall(
                            id="tc3",
                            name="write_file",
                            arguments={"path": "math_lib.py", "content": "def add(a, b):\n    return a + b\n"},
                        )
                    ],
                    done=True,
                )
            elif self.turn == 3:
                # 3. Test execution
                return GenerationResult(
                    model="test",
                    tool_calls=[
                        ToolCall(
                            id="tc4",
                            name="run_command",
                            arguments={"argv": ["python", "-c", "import sys; sys.exit(0)"]},
                        )
                    ],
                    done=True,
                )
            else:
                # 4. Verified final report
                return GenerationResult(
                    model="test",
                    content="Verified outcome: Fixed addition in math_lib.py. Tests ran and passed.",
                    done=True,
                )

    service = AgentService(tmp_path, WorkflowProvider())
    req = AgentTaskRequest(
        model="test",
        messages=[ChatMessage(role="user", content="Fix the bug in add function")],
        session_id="0123456789abcdef0123456789abcdef",
    )
    result = run(
        service.run_autonomous(
            req,
            approve_write=lambda p: True,
            approve_command=lambda p: True,
            policy=ExecutionPolicy.NORMAL,
        )
    )

    assert result.generation.content == "Verified outcome: Fixed addition in math_lib.py. Tests ran and passed."
    assert (tmp_path / "math_lib.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"
    assert len(result.tool_outcomes) == 4
    assert result.tool_outcomes[0].name == "search_symbols"
    assert result.tool_outcomes[1].name == "read_file"
    assert result.tool_outcomes[2].name == "write_file"
    assert result.tool_outcomes[3].name == "run_command"
    assert result.tool_outcomes[3].ok is True

    # Checkpoint was recorded for the session
    cp = service.checkpoint_manager.get_checkpoint("0123456789abcdef0123456789abcdef")
    assert cp is not None
    assert "math_lib.py" in cp.snapshots
    assert cp.snapshots["math_lib.py"].pre_sha256 is not None
    assert cp.snapshots["math_lib.py"].post_sha256 is not None
