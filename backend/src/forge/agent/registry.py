from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from forge.agent.policy import ExecutionPolicyConfig
from forge.agent.schemas import (
    FetchUrlArgs,
    InspectResearchArgs,
    ListFilesArgs,
    ReadFileArgs,
    RunCommandArgs,
    SearchDocumentationArgs,
    SearchSymbolsArgs,
    SearchTextArgs,
    WebSearchArgs,
    WriteFileArgs,
)
from forge.context import CheckpointManager, Workspace, WorkspaceError
from forge.context.workspace import WriteProposal
from forge.execution import CommandError, CommandProposal, CommandRunner
from forge.providers.models import ToolDefinition, ToolResult
from forge.research import (
    FetcherError,
    ResearchQuery,
    ResearchService,
    escape_untrusted_content,
    format_evidence_for_prompt,
)
from forge.research.providers.base import SearchError


@dataclass
class ToolExecutionContext:
    workspace: Workspace
    command_runner: CommandRunner
    policy_config: ExecutionPolicyConfig
    approve_write: Callable[[WriteProposal], bool]
    approve_command: Callable[[CommandProposal], bool]
    checkpoint_manager: CheckpointManager | None = None
    task_id: str | None = None
    research_service: Any = None


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


async def _handle_web_search(args: WebSearchArgs, ctx: ToolExecutionContext) -> ToolResult:
    try:
        service = ctx.research_service or ResearchService(ctx.workspace.root)
        query = ResearchQuery(
            query=args.query,
            limit=args.limit,
            domain_restrictions=args.domains,
            recency=args.recency,
        )
        report = await service.research(query)
        evidence_text = format_evidence_for_prompt(report.sources, report.evidence_excerpts)
        summary = (
            f"Research report ID: {report.research_id}\n"
            f"Query: {report.query.query}\n"
            f"Sources retrieved: {len(report.sources)}\n\n"
            f"{evidence_text}"
        )
        return ToolResult(name="web_search", ok=True, content=summary[:64_000])
    except (SearchError, FetcherError, ValueError) as error:
        return ToolResult(
            name="web_search",
            ok=False,
            content=getattr(error, "public_message", str(error)),
        )


async def _handle_fetch_url(args: FetchUrlArgs, ctx: ToolExecutionContext) -> ToolResult:
    try:
        service = ctx.research_service or ResearchService(ctx.workspace.root)
        doc = await service.fetch_url(args.url)
        content_preview = escape_untrusted_content(doc.extracted_text[:args.max_chars])
        clean_title = escape_untrusted_content(doc.title)
        clean_url = escape_untrusted_content(doc.canonical_url)
        clean_domain = escape_untrusted_content(doc.domain)
        clean_type = escape_untrusted_content(doc.content_type)
        formatted = (
            f"Title: {clean_title}\n"
            f"Canonical URL: {clean_url}\n"
            f"Domain: {clean_domain}\n"
            f"Content Type: {clean_type}\n"
            f"Status: {doc.status}\n\n"
            f"<untrusted_retrieved_content url=\"{clean_url}\">\n"
            f"{content_preview}\n"
            f"</untrusted_retrieved_content>"
        )
        return ToolResult(name="fetch_url", ok=True, content=formatted[:64_000])
    except (FetcherError, ValueError) as error:
        return ToolResult(
            name="fetch_url",
            ok=False,
            content=getattr(error, "public_message", str(error)),
        )


async def _handle_search_documentation(args: SearchDocumentationArgs, ctx: ToolExecutionContext) -> ToolResult:
    try:
        service = ctx.research_service or ResearchService(ctx.workspace.root)
        q_text = args.query
        if args.package and args.package.lower() not in q_text.lower():
            q_text = f"{args.package} {q_text}"
        if args.version and args.version.lower() not in q_text.lower():
            q_text = f"{q_text} {args.version}"

        query = ResearchQuery(
            query=q_text,
            limit=args.limit,
            domain_restrictions=args.domains,
            package=args.package,
            version=args.version,
        )
        report = await service.research(query)
        evidence_text = format_evidence_for_prompt(report.sources, report.evidence_excerpts)
        summary = (
            f"Documentation research report ID: {report.research_id}\n"
            f"Query: {q_text}\n"
            f"Sources retrieved: {len(report.sources)}\n\n"
            f"{evidence_text}"
        )
        return ToolResult(name="search_documentation", ok=True, content=summary[:64_000])
    except (SearchError, FetcherError, ValueError) as error:
        return ToolResult(
            name="search_documentation",
            ok=False,
            content=getattr(error, "public_message", str(error)),
        )


async def _handle_inspect_research(args: InspectResearchArgs, ctx: ToolExecutionContext) -> ToolResult:
    try:
        service = ctx.research_service or ResearchService(ctx.workspace.root)
        report = service.get_report(args.research_id)
        if report is None:
            return ToolResult(
                name="inspect_research",
                ok=False,
                content=f"Research report '{args.research_id}' not found.",
            )

        sources = report.sources
        excerpts = report.evidence_excerpts
        if args.source_id:
            sources = [s for s in sources if s.source_id == args.source_id]
            excerpts = [e for e in excerpts if e.source_id == args.source_id]

        evidence_text = format_evidence_for_prompt(sources, excerpts)
        return ToolResult(name="inspect_research", ok=True, content=evidence_text[:64_000])
    except Exception as error:
        return ToolResult(
            name="inspect_research",
            ok=False,
            content=str(error),
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

    registry.register(
        RegisteredTool(
            name="web_search",
            description="Search the web for external documentation and technical information.",
            read_only=True,
            args_schema=WebSearchArgs,
            definition=ToolDefinition(
                name="web_search",
                description="Search the web for external documentation and technical information.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query to find external technical documentation.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of search results to retrieve.",
                            "default": 5,
                        },
                        "domains": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional list of domain restrictions.",
                        },
                        "recency": {
                            "type": "string",
                            "description": "Optional recency filter (day, week, month, year).",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            handler=_handle_web_search,
        )
    )

    registry.register(
        RegisteredTool(
            name="fetch_url",
            description="Safely retrieve and extract text from a specific documentation URL.",
            read_only=True,
            args_schema=FetchUrlArgs,
            definition=ToolDefinition(
                name="fetch_url",
                description="Safely retrieve and extract text from a specific documentation URL.",
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "HTTP or HTTPS URL of documentation page to safely retrieve.",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": "Maximum characters of extracted content to return.",
                            "default": 12000,
                        },
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
            ),
            handler=_handle_fetch_url,
        )
    )

    registry.register(
        RegisteredTool(
            name="search_documentation",
            description="Search documentation for a package, framework, API, or language feature.",
            read_only=True,
            args_schema=SearchDocumentationArgs,
            definition=ToolDefinition(
                name="search_documentation",
                description="Search documentation for a package, framework, API, or language feature.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Documentation topic, API, function, or framework feature to search for.",
                        },
                        "package": {
                            "type": "string",
                            "description": "Optional package or library name (e.g. fastapi, pydantic).",
                        },
                        "version": {
                            "type": "string",
                            "description": "Optional package version to target.",
                        },
                        "domains": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional list of domain restrictions.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of documentation sources to retrieve.",
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            handler=_handle_search_documentation,
        )
    )

    registry.register(
        RegisteredTool(
            name="inspect_research",
            description="Inspect a previously generated research report and its evidence.",
            read_only=True,
            args_schema=InspectResearchArgs,
            definition=ToolDefinition(
                name="inspect_research",
                description="Inspect a previously generated research report and its evidence.",
                parameters={
                    "type": "object",
                    "properties": {
                        "research_id": {
                            "type": "string",
                            "description": "Unique identifier of previously generated research report.",
                        },
                        "source_id": {
                            "type": "string",
                            "description": "Optional source ID filter (e.g. src_1).",
                        },
                    },
                    "required": ["research_id"],
                    "additionalProperties": False,
                },
            ),
            handler=_handle_inspect_research,
        )
    )

    return registry
