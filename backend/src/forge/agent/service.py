from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from forge.context import Workspace, WorkspaceError
from forge.providers import ModelProvider
from forge.providers.models import ChatMessage, GenerationRequest, GenerationResult

MAX_CONTEXT_FILES = 10
MAX_CONTEXT_FILE_CHARS = 12_000
MAX_CONTEXT_CHARS = 60_000


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


class AgentService:
    """Build bounded, read-only workspace context for a provider request."""

    def __init__(self, workspace_root: Path, provider: ModelProvider) -> None:
        self._workspace = Workspace(workspace_root)
        self._provider = provider

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
