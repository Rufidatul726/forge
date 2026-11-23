import json
import logging
from collections.abc import Sequence
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("forge.agent.policy")


class ExecutionPolicy(str, Enum):
    PLAN = "plan"
    NORMAL = "normal"
    AUTONOMOUS = "autonomous"


class ExecutionPolicyConfig(BaseModel):
    policy: ExecutionPolicy = ExecutionPolicy.NORMAL
    allowed_autonomous_tools: set[str] = Field(default_factory=set)

    def is_permitted(self, tool_name: str, read_only: bool) -> tuple[bool, str | None]:
        if self.policy == ExecutionPolicy.PLAN:
            if not read_only:
                return False, f"Action '{tool_name}' is not permitted in plan mode (read-only)."
            return True, None

        if self.policy == ExecutionPolicy.AUTONOMOUS:
            if read_only:
                return True, None
            if tool_name not in self.allowed_autonomous_tools:
                return (
                    False,
                    f"Action '{tool_name}' is not permitted in autonomous mode without explicit permission.",
                )
            return True, None

        # NORMAL policy permits read and mutating actions (mutating actions require approval)
        return True, None

    def requires_approval(self, tool_name: str, read_only: bool) -> bool:
        if read_only:
            return False
        if self.policy == ExecutionPolicy.PLAN:
            return False  # Blocked before approval
        if self.policy == ExecutionPolicy.AUTONOMOUS:
            return False  # Autonomous actions with explicit permission do not prompt
        return True  # NORMAL policy requires approval for writes and commands


def _load_config_from_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception as exc:
        logger.warning("Failed to read policy configuration from %s: %s", path, exc)
    return None


def load_execution_policy_config(
    workspace_root: Path | None = None,
    *,
    global_config_path: Path | None = None,
    project_config_path: Path | None = None,
    cli_policy: ExecutionPolicy | None = None,
    cli_allowed_tools: Sequence[str] | None = None,
) -> ExecutionPolicyConfig:
    """Load execution policy from global config, overlay project config, and apply CLI overrides."""
    policy = ExecutionPolicy.NORMAL
    allowed_tools: set[str] = set()

    # 1. Global config (~/.forge/config.json)
    gpath = global_config_path or (Path.home() / ".forge" / "config.json")
    global_data = _load_config_from_file(gpath)
    if global_data:
        p_val = global_data.get("policy") or global_data.get("execution_policy")
        if p_val and isinstance(p_val, str):
            try:
                policy = ExecutionPolicy(p_val.lower())
            except ValueError:
                pass
        tools_val = global_data.get("allowed_autonomous_tools")
        if isinstance(tools_val, list):
            allowed_tools.update(str(t) for t in tools_val if isinstance(t, str))

    # 2. Project config (<workspace>/.forge/config.json)
    ppath = project_config_path
    if ppath is None and workspace_root is not None:
        ppath = workspace_root / ".forge" / "config.json"
    if ppath:
        project_data = _load_config_from_file(ppath)
        if project_data:
            p_val = project_data.get("policy") or project_data.get("execution_policy")
            if p_val and isinstance(p_val, str):
                try:
                    policy = ExecutionPolicy(p_val.lower())
                except ValueError:
                    pass
            tools_val = project_data.get("allowed_autonomous_tools")
            if isinstance(tools_val, list):
                allowed_tools.update(str(t) for t in tools_val if isinstance(t, str))

    # 3. CLI / programmatic overrides
    if cli_policy is not None:
        policy = cli_policy
    if cli_allowed_tools is not None:
        allowed_tools.update(cli_allowed_tools)

    return ExecutionPolicyConfig(
        policy=policy,
        allowed_autonomous_tools=allowed_tools,
    )

