import json
import re
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from forge.agent.dispatcher import ToolDispatcher
from forge.agent.policy import ExecutionPolicy, ExecutionPolicyConfig
from forge.agent.registry import (
    ToolExecutionContext,
    ToolRegistry,
    create_default_registry,
)
from forge.context import CheckpointManager, Workspace
from forge.context.workspace import WriteProposal
from forge.execution import CommandProposal, CommandRunner
from forge.providers import ModelProvider
from forge.providers.models import (
    ChatMessage,
    GenerationChunk,
    GenerationRequest,
    GenerationResult,
    ProviderResponse,
    ToolCall,
)

MAX_CONTEXT_FILES = 10
MAX_CONTEXT_FILE_CHARS = 12_000
MAX_CONTEXT_CHARS = 60_000
MAX_AGENT_STEPS = 8


class AgentTaskRequest(BaseModel):
    model: str = Field(min_length=1, max_length=255)
    messages: list[ChatMessage] = Field(min_length=1, max_length=32)
    context_paths: list[str] = Field(default_factory=list, max_length=MAX_CONTEXT_FILES)
    session_id: str | None = Field(default=None, min_length=32, max_length=32, pattern=r"^[0-9a-f]+$")
    max_tokens: int = Field(default=1024, gt=0, le=8192)

    @field_validator("model")
    @classmethod
    def model_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Model name must not be blank")
        return value.strip()


class AgentToolCall(ToolCall):
    pass


class AgentToolOutcome(BaseModel):
    name: str
    ok: bool
    result: str = Field(max_length=64_000)


class AgentTaskResult(BaseModel):
    generation: ProviderResponse
    workspace_root: str
    context_paths: list[str]
    session_id: str | None = None
    read_only: bool = True
    tool_outcomes: list[AgentToolOutcome] = Field(default_factory=list, exclude=True)


class AgentService:
    """Build repository context and run bounded repository tasks with native tool calling."""

    def __init__(
        self,
        workspace_root: Path,
        provider: ModelProvider,
        *,
        registry: ToolRegistry | None = None,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self._workspace = Workspace(workspace_root)
        self._provider = provider
        self._command_runner = command_runner or CommandRunner(workspace_root)
        self._registry = registry or create_default_registry()
        self._checkpoint_manager = CheckpointManager(workspace_root)

    @property
    def checkpoint_manager(self) -> CheckpointManager:
        return self._checkpoint_manager

    async def run_autonomous(
        self,
        task: AgentTaskRequest,
        *,
        approve_write: Callable[[WriteProposal], bool],
        approve_command: Callable[[CommandProposal], bool],
        policy: ExecutionPolicy = ExecutionPolicy.NORMAL,
        policy_config: ExecutionPolicyConfig | None = None,
        max_steps: int = MAX_AGENT_STEPS,
    ) -> AgentTaskResult:
        if not 1 <= max_steps <= MAX_AGENT_STEPS:
            raise ValueError(f"Agent steps must be between 1 and {MAX_AGENT_STEPS}")

        active_policy = policy_config or ExecutionPolicyConfig(policy=policy)
        context = self._build_context(task.context_paths)
        task_id = task.session_id or uuid4().hex

        context_obj = ToolExecutionContext(
            workspace=self._workspace,
            command_runner=self._command_runner,
            policy_config=active_policy,
            approve_write=approve_write,
            approve_command=approve_command,
            checkpoint_manager=self._checkpoint_manager,
            task_id=task_id,
        )
        dispatcher = ToolDispatcher(self._registry, context_obj)

        policy_note = ""
        if active_policy.policy == ExecutionPolicy.PLAN:
            policy_note = "\nYou are in PLAN mode: read-only actions are permitted; write and command actions are forbidden."

        system_message = ChatMessage(
            role="system",
            content=(
                "You are Forge, an autonomous repository assistant. Use native tools only when "
                "needed to inspect the workspace, search text, write files, or run allowlisted commands. "
                "Never claim a mutation or command succeeded unless its tool result confirms it."
                f"{policy_note}\n\n"
                f"{context}"
            ),
        )
        messages = [system_message, *task.messages]
        outcomes: list[AgentToolOutcome] = []
        tools = self._registry.get_definitions()
        is_read_only = active_policy.policy == ExecutionPolicy.PLAN

        for _ in range(max_steps):
            response = await self._provider.generate(
                GenerationRequest(
                    model=task.model,
                    messages=messages,
                    max_tokens=task.max_tokens,
                    tools=tools,
                )
            )

            tool_calls = list(response.tool_calls)
            # If native tool calls are empty, check for legacy text-based tool calls as fallback
            if not tool_calls and response.content:
                parsed_call = self._parse_legacy_tool_call(response.content)
                if parsed_call is not None:
                    tool_calls.append(parsed_call)

            if not tool_calls:
                return AgentTaskResult(
                    generation=response,
                    workspace_root=str(self._workspace.root),
                    context_paths=task.context_paths,
                    read_only=is_read_only,
                    tool_outcomes=outcomes,
                )

            # Record assistant turn with tool calls
            messages.append(
                ChatMessage(
                    role="assistant",
                    content=response.content,
                    tool_calls=tool_calls,
                )
            )

            # Dispatch each tool call sequentially
            for call in tool_calls:
                tool_result = await dispatcher.dispatch(call)
                outcomes.append(
                    AgentToolOutcome(
                        name=tool_result.name,
                        ok=tool_result.ok,
                        result=tool_result.content,
                    )
                )
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=tool_result.content,
                        name=tool_result.name,
                        tool_call_id=call.id,
                    )
                )

        # Max steps reached: prompt provider for a concise summary without tools
        final_response = await self._provider.generate(
            GenerationRequest(
                model=task.model,
                messages=[
                    *messages,
                    ChatMessage(
                        role="user",
                        content="The tool step limit was reached. Return only a concise summary "
                        "of completed work and remaining blocked work.",
                    ),
                ],
                max_tokens=task.max_tokens,
            )
        )
        return AgentTaskResult(
            generation=final_response,
            workspace_root=str(self._workspace.root),
            context_paths=task.context_paths,
            read_only=is_read_only,
            tool_outcomes=outcomes,
        )

    async def run_loop(
        self,
        task: AgentTaskRequest,
        *,
        approve_write: Callable[[WriteProposal], bool],
        approve_command: Callable[[CommandProposal], bool],
        policy: ExecutionPolicy = ExecutionPolicy.NORMAL,
        policy_config: ExecutionPolicyConfig | None = None,
        max_steps: int = MAX_AGENT_STEPS,
    ) -> AgentTaskResult:
        return await self.run_autonomous(
            task,
            approve_write=approve_write,
            approve_command=approve_command,
            policy=policy,
            policy_config=policy_config,
            max_steps=max_steps,
        )

    async def run(self, task: AgentTaskRequest) -> AgentTaskResult:
        context = self._build_context(task.context_paths)
        system_message = ChatMessage(
            role="system",
            content=(
                "You are Forge, a read-only repository assistant. Answer using the supplied "
                "workspace context when relevant. Do not claim to have modified files, run "
                "commands, or inspected files that are not included in the context.\n\n"
                f"{context}"
            ),
        )
        generation = await self._provider.generate(
            GenerationRequest(
                model=task.model,
                messages=[system_message, *task.messages],
                max_tokens=task.max_tokens,
            )
        )
        return AgentTaskResult(
            generation=generation,
            workspace_root=str(self._workspace.root),
            context_paths=task.context_paths,
            read_only=True,
        )

    async def stream(
        self, task: AgentTaskRequest, *, context: str | None = None
    ) -> AsyncIterator[GenerationChunk]:
        if context is None:
            context = self._build_context(task.context_paths)
        system_message = ChatMessage(
            role="system",
            content=(
                "You are Forge, a read-only repository assistant. Answer using the supplied "
                "workspace context when relevant. Do not claim to have modified files, run "
                "commands, or inspected files that are not included in the context.\n\n"
                f"{context}"
            ),
        )
        request = GenerationRequest(
            model=task.model,
            messages=[system_message, *task.messages],
            max_tokens=task.max_tokens,
        )
        if hasattr(self._provider, "stream_generate"):
            async for chunk in self._provider.stream_generate(request):
                yield chunk
        else:
            result = await self._provider.generate(request)
            yield GenerationChunk(content=result.content, done=True, model=result.model)

    def _parse_legacy_tool_call(self, content: str) -> ToolCall | None:
        candidates: list[str] = []
        marked = re.findall(
            r"<forge_tool_call>\s*(.*?)\s*</forge_tool_call>",
            content,
            flags=re.DOTALL | re.IGNORECASE,
        )
        candidates.extend(marked)
        fenced = re.findall(r"```(?:json)?\s*(.*?)\s*```", content, flags=re.DOTALL | re.I)
        candidates.extend(fenced)

        for candidate in candidates:
            candidate = candidate.strip()
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            call = self._normalize_legacy_payload(payload)
            if call is not None:
                return call

        try:
            payload = json.loads(content.strip())
        except json.JSONDecodeError:
            return None
        return self._normalize_legacy_payload(payload)

    def _normalize_legacy_payload(self, payload: object) -> ToolCall | None:
        if not isinstance(payload, Mapping):
            return None
        name = payload.get("name", payload.get("tool"))
        arguments = payload.get("arguments", payload.get("args", {}))
        if not isinstance(name, str) or not isinstance(arguments, Mapping):
            return None
        return ToolCall(name=name, arguments=dict(arguments))

    def _build_context(self, context_paths: list[str], max_token_budget: int = 6000) -> str:
        discovered = self._workspace.discover_context(file_limit=200)
        char_budget = max_token_budget * 4

        sections: list[str] = []

        # Instructions (root + nested)
        if discovered.instructions:
            inst_budget = int(char_budget * 0.3)
            inst_text = discovered.instructions[:inst_budget]
            sections.append(f"Project instructions:\n{inst_text}")

        # Workspace file list
        files_budget = int(char_budget * 0.25)
        files_text = "\n".join(discovered.files.paths)[:files_budget]
        sections.append(f"Workspace file list:\n{files_text}")

        # Remaining budget for context files
        used_chars = sum(len(s) for s in sections)
        remaining_chars = max(0, char_budget - used_chars)

        if context_paths and remaining_chars > 0:
            per_file_chars = max(500, remaining_chars // len(context_paths))
            for path in context_paths:
                if remaining_chars <= 0:
                    break
                content = self._workspace.read_text(path)
                take_chars = min(per_file_chars, len(content), remaining_chars)
                excerpt = content[:take_chars]
                sections.append(f"File: {path}\n{excerpt}")
                remaining_chars -= len(sections[-1])

        return "\n\n".join(sections)[:char_budget]
