from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


class ToolDefinition(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    description: str = Field(default="", max_length=2048)
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )


class ToolCall(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    name: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    call_id: str | None = None
    name: str = Field(min_length=1, max_length=64)
    content: str = Field(default="", max_length=64_000)
    ok: bool = True


class AssistantMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str = Field(default="", max_length=64_000)
    tool_calls: list[ToolCall] = Field(default_factory=list)


class ProviderResponse(BaseModel):
    model: str = Field(min_length=1, max_length=255)
    content: str = Field(default="", max_length=64_000)
    message: AssistantMessage | None = Field(default=None, exclude=True)
    tool_calls: list[ToolCall] = Field(default_factory=list, exclude=True)
    done: bool = True

    def model_post_init(self, __context: Any) -> None:
        if self.message is None:
            object.__setattr__(
                self,
                "message",
                AssistantMessage(
                    role="assistant",
                    content=self.content,
                    tool_calls=list(self.tool_calls),
                ),
            )
        else:
            if not self.content and self.message.content:
                object.__setattr__(self, "content", self.message.content)
            if not self.tool_calls and self.message.tool_calls:
                object.__setattr__(self, "tool_calls", list(self.message.tool_calls))


class GenerationResult(ProviderResponse):
    pass


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(default="", max_length=64_000)
    name: str | None = Field(default=None, max_length=64)
    tool_call_id: str | None = Field(default=None, max_length=128)
    tool_calls: list[ToolCall] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content_or_tool_calls(self) -> "ChatMessage":
        if not self.content and not self.tool_calls:
            raise ValueError("Message must have non-empty content or at least one tool call")
        return self


class GenerationRequest(BaseModel):
    model: str = Field(min_length=1, max_length=255)
    messages: list[ChatMessage] = Field(min_length=1, max_length=64)
    max_tokens: int = Field(default=1024, gt=0, le=8192)
    tools: list[ToolDefinition] = Field(default_factory=list)

    @field_validator("model")
    @classmethod
    def model_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Model name must not be blank")
        return value.strip()


class GenerationChunk(BaseModel):
    content: str
    done: bool
    model: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
