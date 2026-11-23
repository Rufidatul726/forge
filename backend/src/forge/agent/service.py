import json
import re
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from forge.context import Workspace, WorkspaceError
from forge.context.workspace import WriteProposal
from forge.execution import CommandError, CommandProposal, CommandRunner
from forge.providers import ModelProvider
from forge.providers.models import (
    ChatMessage,
    GenerationChunk,
    GenerationRequest,
    GenerationResult,
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


class AgentTaskResult(BaseModel):
    generation: GenerationResult
    workspace_root: str
    context_paths: list[str]
    session_id: str | None = None
    read_only: bool = True
    tool_outcomes: list["AgentToolOutcome"] = Field(default_factory=list, exclude=True)


class AgentToolCall(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    arguments: dict[str, object] = Field(default_factory=dict)


class AgentToolOutcome(BaseModel):
    name: str
    ok: bool
    result: str = Field(max_length=64_000)


class AgentService:
    """Build repository context and run bounded repository tasks."""

    def __init__(self, workspace_root: Path, provider: ModelProvider) -> None:
        self._workspace = Workspace(workspace_root)
        self._provider = provider
        self._command_runner = CommandRunner(workspace_root)

    async def run_autonomous(
        self,
        task: AgentTaskRequest,
        *,
        approve_write: Callable[[WriteProposal], bool],
        approve_command: Callable[[CommandProposal], bool],
        max_steps: int = MAX_AGENT_STEPS,
    ) -> AgentTaskResult:
        if not 1 <= max_steps <= MAX_AGENT_STEPS:
            raise ValueError(f"Agent steps must be between 1 and {MAX_AGENT_STEPS}")

        context = self._build_context(task.context_paths)
        system_message = ChatMessage(
            role="system",
            content=(
                "You are Forge, an autonomous repository assistant. Use tools only when "
                "needed, and never claim a mutation or command succeeded unless its tool "
                "result confirms it.\n\n"
                "Emit at most one tool call as a JSON object in a <forge_tool_call> block "
                "and stop when the task is complete. Available tools:\n"
                '- read_file: {\"name\":\"read_file\",\"arguments\":{\"path\":\"src/main.py\"}}\n'
                '- search_text: {\"name\":\"search_text\",\"arguments\":{\"query\":\"needle\",\"pattern\":\"**/*.py\",\"limit\":100}}\n'
                '- write_file: {\"name\":\"write_file\",\"arguments\":{\"path\":\"src/main.py\",\"content\":\"...\"}}\n'
                '- run_command: {\"name\":\"run_command\",\"arguments\":{\"argv\":[\"pytest\",\"-q\"],\"cwd\":\".\",\"timeout_seconds\":30}}\n\n'
                "Writing files and running commands require separate user approval. "
                "Do not emit shell commands, executable paths, or tool calls for unknown tools.\n\n"
                f"{context}"
            ),
        )
        messages = [system_message, *task.messages]
        outcomes: list[AgentToolOutcome] = []

        for _ in range(max_steps):
            generation = await self._provider.generate(
                GenerationRequest(
                    model=task.model,
                    messages=messages,
                    max_tokens=task.max_tokens,
                )
            )
            call = self._parse_tool_call(generation.content)
            if call is None:
                return AgentTaskResult(
                    generation=generation,
                    workspace_root=str(self._workspace.root),
                    context_paths=task.context_paths,
                    read_only=False,
                    tool_outcomes=outcomes,
                )

            messages.append(ChatMessage(role="assistant", content=generation.content))
            outcome = await self._execute_tool_call(
                call,
                approve_write=approve_write,
                approve_command=approve_command,
            )
            outcomes.append(outcome)
            messages.append(
                ChatMessage(
                    role="user",
                    content=(
                        f"<forge_tool_result name=\"{outcome.name}\" "
                        f"ok=\"{str(outcome.ok).lower()}\">{outcome.result}</forge_tool_result>"
                    ),
                )
            )

        generation = await self._provider.generate(
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
            generation=generation,
            workspace_root=str(self._workspace.root),
            context_paths=task.context_paths,
            read_only=False,
            tool_outcomes=outcomes,
        )

    async def run_loop(
        self,
        task: AgentTaskRequest,
        *,
        approve_write: Callable[[WriteProposal], bool],
        approve_command: Callable[[CommandProposal], bool],
        max_steps: int = MAX_AGENT_STEPS,
    ) -> AgentTaskResult:
        return await self.run_autonomous(
            task,
            approve_write=approve_write,
            approve_command=approve_command,
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

    def _parse_tool_call(self, content: str) -> AgentToolCall | None:
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
            call = self._normalize_tool_payload(payload)
            if call is not None:
                return call

        try:
            payload = json.loads(content.strip())
        except json.JSONDecodeError:
            return None
        return self._normalize_tool_payload(payload)

    def _normalize_tool_payload(self, payload: object) -> AgentToolCall | None:
        if not isinstance(payload, Mapping):
            return None
        name = payload.get("name", payload.get("tool"))
        arguments = payload.get("arguments", payload.get("args", {}))
        if not isinstance(name, str) or not isinstance(arguments, Mapping):
            return None
        if name not in {"read_file", "search_text", "write_file", "run_command"}:
            return None
        return AgentToolCall(name=name, arguments=dict(arguments))

    async def _execute_tool_call(
        self,
        call: AgentToolCall,
        *,
        approve_write: Callable[[WriteProposal], bool],
        approve_command: Callable[[CommandProposal], bool],
    ) -> AgentToolOutcome:
        try:
            if call.name == "read_file":
                path = call.arguments.get("path")
                if not isinstance(path, str):
                    raise ValueError("read_file requires a string path")
                content = self._workspace.read_text(path)
                return AgentToolOutcome(name=call.name, ok=True, result=content[:64_000])

            if call.name == "search_text":
                query = call.arguments.get("query")
                pattern = call.arguments.get("pattern", "**/*")
                limit = call.arguments.get("limit", 100)
                case_sensitive = call.arguments.get("case_sensitive", False)
                if not isinstance(query, str):
                    raise ValueError("search_text requires a string query")
                if not isinstance(pattern, str):
                    raise ValueError("search_text pattern must be a string")
                if not isinstance(limit, int) or isinstance(limit, bool):
                    raise ValueError("search_text limit must be an integer")
                if not isinstance(case_sensitive, bool):
                    raise ValueError("search_text case_sensitive must be a boolean")
                results = self._workspace.search_text(
                    query,
                    pattern=pattern,
                    limit=limit,
                    case_sensitive=case_sensitive,
                )
                lines = [
                    f"{match.path}:{match.line_number}: {match.line}"
                    for match in results.matches
                ]
                return AgentToolOutcome(
                    name=call.name,
                    ok=True,
                    result="\n".join(lines) or "No matches found.",
                )

            if call.name == "write_file":
                path = call.arguments.get("path")
                content = call.arguments.get("content")
                if not isinstance(path, str) or not isinstance(content, str):
                    raise ValueError("write_file requires string path and content")
                result = self._workspace.write_text(
                    path,
                    content,
                    approve=approve_write,
                )
                return AgentToolOutcome(
                    name=call.name,
                    ok=True,
                    result=(
                        f"{'Updated' if result.changed else 'No change needed for'} "
                        f"{result.path}; bytes_written={result.bytes_written}"
                    ),
                )

            if call.name == "run_command":
                argv = call.arguments.get("argv")
                cwd = call.arguments.get("cwd", ".")
                timeout = call.arguments.get("timeout_seconds", 30)
                if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
                    raise ValueError("run_command argv must be a list of strings")
                if not isinstance(cwd, str):
                    raise ValueError("run_command cwd must be a string")
                if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
                    raise ValueError("run_command timeout_seconds must be numeric")
                result = await self._command_runner.run(
                    argv,
                    approve=approve_command,
                    cwd=cwd,
                    timeout_seconds=float(timeout),
                )
                result_text = (
                    f"exit_code={result.exit_code}; timed_out={str(result.timed_out).lower()}\n"
                    f"stdout:\n{result.stdout}\n"
                    f"stderr:\n{result.stderr}"
                )
                return AgentToolOutcome(
                    name=call.name,
                    ok=result.exit_code == 0 and not result.timed_out,
                    result=result_text,
                )

            raise ValueError(f"Unknown tool: {call.name}")
        except (WorkspaceError, CommandError, ValueError) as error:
            return AgentToolOutcome(
                name=call.name,
                ok=False,
                result=getattr(error, "public_message", str(error)),
            )

    def _build_context(self, context_paths: list[str]) -> str:
        discovered = self._workspace.discover_context(file_limit=200)
        sections = [
            "Workspace file list:\n" + "\n".join(discovered.files.paths),
        ]
        if discovered.instructions:
            sections.append(f"Project instructions (FORGE.md):\n{discovered.instructions}")

        remaining = MAX_CONTEXT_CHARS - sum(len(section) for section in sections)
        for path in context_paths:
            if remaining <= 0:
                break
            content = self._workspace.read_text(path)
            excerpt = content[: min(MAX_CONTEXT_FILE_CHARS, remaining)]
            sections.append(f"File: {path}\n{excerpt}")
            remaining -= len(sections[-1])
        return "\n\n".join(sections)[:MAX_CONTEXT_CHARS]
