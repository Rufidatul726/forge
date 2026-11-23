from pydantic import ValidationError

from forge.agent.registry import ToolExecutionContext, ToolRegistry
from forge.providers.models import ToolCall, ToolResult


class ToolDispatcher:
    def __init__(self, registry: ToolRegistry, context: ToolExecutionContext) -> None:
        self._registry = registry
        self._context = context

    async def dispatch(self, tool_call: ToolCall) -> ToolResult:
        tool = self._registry.get(tool_call.name)
        if tool is None:
            available = ", ".join(sorted(self._registry.list_tools()))
            return ToolResult(
                call_id=tool_call.id,
                name=tool_call.name,
                ok=False,
                content=f"Unknown tool: '{tool_call.name}'. Available tools: {available}",
            )

        # Validate arguments against the strict schema
        try:
            validated_args = tool.args_schema.model_validate(tool_call.arguments)
        except ValidationError as exc:
            return ToolResult(
                call_id=tool_call.id,
                name=tool_call.name,
                ok=False,
                content=f"Invalid arguments for tool '{tool_call.name}': {exc}",
            )

        # Check execution policy
        permitted, reason = self._context.policy_config.is_permitted(
            tool.name, tool.read_only
        )
        if not permitted:
            return ToolResult(
                call_id=tool_call.id,
                name=tool_call.name,
                ok=False,
                content=reason or f"Tool '{tool_call.name}' is not permitted by current execution policy.",
            )

        # Execute handler
        result = await tool.handler(validated_args, self._context)
        return ToolResult(
            call_id=tool_call.id,
            name=result.name,
            ok=result.ok,
            content=result.content,
        )
