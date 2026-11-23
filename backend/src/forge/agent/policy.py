from enum import Enum
from pydantic import BaseModel, Field


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
