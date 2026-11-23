from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from forge.agent.policy import ExecutionPolicyConfig
from forge.agent.schemas import (
    ListFilesArgs,
    ReadFileArgs,
    RunCommandArgs,
    SearchSymbolsArgs,
    SearchTextArgs,
    WriteFileArgs,
)
from forge.context import CheckpointManager, Workspace, WorkspaceError
from forge.context.workspace import WriteProposal
from forge.execution import CommandError, CommandProposal, CommandRunner
from forge.providers.models import ToolDefinition, ToolResult


@dataclass
class ToolExecutionContext:
    workspace: Workspace
    command_runner: CommandRunner
    policy_config: ExecutionPolicyConfig
    approve_write: Callable[[WriteProposal], bool]
    approve_command: Callable[[CommandProposal], bool]
    checkpoint_manager: CheckpointManager | None = None
    task_id: str | None = None


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    description: str
    read_only: bool
    args_schema: type[BaseModel]
    definition: ToolDefinition
    handler: Callable[[Any, ToolExecutionContext], Awaitable[ToolResult]]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, tool: RegisteredTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

    def get_definitions(self) -> list[ToolDefinition]:
        return [tool.definition for tool in self._tools.values()]


async def _handle_read_file(args: ReadFileArgs, ctx: ToolExecutionContext) -> ToolResult:
    try:
        content = ctx.workspace.read_text(args.path)
        return ToolResult(name="read_file", ok=True, content=content[:64_000])
    except WorkspaceError as error:
        return ToolResult(
            name="read_file",
            ok=False,
            content=getattr(error, "public_message", str(error)),
        )


async def _handle_search_text(args: SearchTextArgs, ctx: ToolExecutionContext) -> ToolResult:
    try:
        results = ctx.workspace.search_text(
            args.query,
            pattern=args.pattern,
            limit=args.limit,
            case_sensitive=args.case_sensitive,
        )
        lines = [f"{m.path}:{m.line_number}: {m.line}" for m in results.matches]
        return ToolResult(
            name="search_text",
            ok=True,
            content="\n".join(lines) or "No matches found.",
        )
    except WorkspaceError as error:
        return ToolResult(
            name="search_text",
            ok=False,
            content=getattr(error, "public_message", str(error)),
        )


async def _handle_write_file(args: WriteFileArgs, ctx: ToolExecutionContext) -> ToolResult:
    try:
        approve = (
            ctx.approve_write
            if ctx.policy_config.requires_approval("write_file", read_only=False)
            else (lambda p: True)
        )

        # Capture pre-change content before any modification occurs
        pre_content: str | None = None
        if ctx.checkpoint_manager and ctx.task_id:
            try:
                pre_content = ctx.workspace.read_text(args.path)
            except WorkspaceError:
                pre_content = None

        result = ctx.workspace.write_text(args.path, args.content, approve=approve)

        # Only record checkpoint if the write succeeded and actually changed the file
        if result.changed and ctx.checkpoint_manager and ctx.task_id:
            ctx.checkpoint_manager.record_pre_change(ctx.task_id, args.path, pre_content)
            try:
                actual_post_content = ctx.workspace.read_text(args.path)
                ctx.checkpoint_manager.record_post_change(
                    ctx.task_id, args.path, actual_post_content
                )
            except WorkspaceError:
                pass

        return ToolResult(
            name="write_file",
            ok=True,
            content=(
                f"{'Updated' if result.changed else 'No change needed for'} "
                f"{result.path}; bytes_written={result.bytes_written}"
            ),
        )
    except WorkspaceError as error:
        return ToolResult(
            name="write_file",
            ok=False,
            content=getattr(error, "public_message", str(error)),
        )


async def _handle_search_symbols(args: SearchSymbolsArgs, ctx: ToolExecutionContext) -> ToolResult:
    try:
        results = ctx.workspace.search_symbols(
            args.query,
            pattern=args.pattern,
            limit=args.limit,
        )
        lines = [
            f"{m.path}:{m.line_number} [{m.kind}] {m.name} -> {m.line}"
            for m in results.matches
        ]
        return ToolResult(
            name="search_symbols",
            ok=True,
            content="\n".join(lines) or "No matching symbols found.",
        )
    except WorkspaceError as error:
        return ToolResult(
            name="search_symbols",
            ok=False,
            content=getattr(error, "public_message", str(error)),
        )


async def _handle_list_files(args: ListFilesArgs, ctx: ToolExecutionContext) -> ToolResult:
    try:
        listing = ctx.workspace.list_files(pattern=args.pattern, limit=args.limit)
        return ToolResult(
            name="list_files",
            ok=True,
            content="\n".join(listing.paths) or "No matching files found.",
        )
    except WorkspaceError as error:
        return ToolResult(
            name="list_files",
            ok=False,
            content=getattr(error, "public_message", str(error)),
        )


async def _handle_run_command(args: RunCommandArgs, ctx: ToolExecutionContext) -> ToolResult:
    try:
        approve = (
            ctx.approve_command
            if ctx.policy_config.requires_approval("run_command", read_only=False)
            else (lambda p: True)
        )
        result = await ctx.command_runner.run(
            args.argv,
            approve=approve,
            cwd=args.cwd,
            timeout_seconds=args.timeout_seconds,
        )
        result_text = (
            f"exit_code={result.exit_code}; timed_out={str(result.timed_out).lower()}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
        return ToolResult(
            name="run_command",
            ok=result.exit_code == 0 and not result.timed_out,
            content=result_text,
        )
    except (CommandError, ValueError) as error:
        return ToolResult(
            name="run_command",
            ok=False,
            content=getattr(error, "public_message", str(error)),
        )


def create_default_registry() -> ToolRegistry:
    registry = ToolRegistry()

    registry.register(
        RegisteredTool(
            name="read_file",
            description="Read UTF-8 text contents of a file within the workspace.",
            read_only=True,
            args_schema=ReadFileArgs,
            definition=ToolDefinition(
                name="read_file",
                description="Read UTF-8 text contents of a file within the workspace.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path to the workspace file to read.",
                        }
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            ),
            handler=_handle_read_file,
        )
    )

    registry.register(
        RegisteredTool(
            name="search_text",
            description="Search for text across readable workspace files.",
            read_only=True,
            args_schema=SearchTextArgs,
            definition=ToolDefinition(
                name="search_text",
                description="Search for text across readable workspace files.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Text substring or token to search for in files.",
                        },
                        "pattern": {
                            "type": "string",
                            "description": "Glob pattern to match file paths against.",
                            "default": "**/*",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of line matches to return.",
                            "default": 100,
                        },
                        "case_sensitive": {
                            "type": "boolean",
                            "description": "Whether to perform case-sensitive matching.",
                            "default": False,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            handler=_handle_search_text,
        )
    )

    registry.register(
        RegisteredTool(
            name="write_file",
            description="Write UTF-8 text content to a workspace file. Requires user approval.",
            read_only=False,
            args_schema=WriteFileArgs,
            definition=ToolDefinition(
                name="write_file",
                description="Write UTF-8 text content to a workspace file. Requires user approval.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path to the workspace file to write.",
                        },
                        "content": {
                            "type": "string",
                            "description": "Text content to write to the file.",
                        },
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
            ),
            handler=_handle_write_file,
        )
    )

    registry.register(
        RegisteredTool(
            name="search_symbols",
            description="Search for programming language symbols (classes, functions, methods, interfaces, etc.) in the workspace.",
            read_only=True,
            args_schema=SearchSymbolsArgs,
            definition=ToolDefinition(
                name="search_symbols",
                description="Search for programming language symbols (classes, functions, methods, interfaces, etc.) in the workspace.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Symbol name substring to search for (function, class, method, etc.).",
                        },
                        "pattern": {
                            "type": "string",
                            "description": "Glob pattern to filter code files.",
                            "default": "**/*",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of symbol matches to return.",
                            "default": 50,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            handler=_handle_search_symbols,
        )
    )

    registry.register(
        RegisteredTool(
            name="list_files",
            description="List workspace files matching a glob pattern.",
            read_only=True,
            args_schema=ListFilesArgs,
            definition=ToolDefinition(
                name="list_files",
                description="List workspace files matching a glob pattern.",
                parameters={
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Glob pattern to filter file paths.",
                            "default": "**/*",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of files to return.",
                            "default": 100,
                        },
                    },
                    "additionalProperties": False,
                },
            ),
            handler=_handle_list_files,
        )
    )

    registry.register(
        RegisteredTool(
            name="run_command",
            description="Execute an allowlisted development command without shell. Requires user approval.",
            read_only=False,
            args_schema=RunCommandArgs,
            definition=ToolDefinition(
                name="run_command",
                description="Execute an allowlisted development command without shell. Requires user approval.",
                parameters={
                    "type": "object",
                    "properties": {
                        "argv": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Command arguments list, where argv[0] is the executable name.",
                        },
                        "cwd": {
                            "type": "string",
                            "description": "Workspace-relative working directory.",
                            "default": ".",
                        },
                        "timeout_seconds": {
                            "type": "number",
                            "description": "Maximum execution timeout in seconds.",
                            "default": 30.0,
                        },
                    },
                    "required": ["argv"],
                    "additionalProperties": False,
                },
            ),
            handler=_handle_run_command,
        )
    )

    return registry
