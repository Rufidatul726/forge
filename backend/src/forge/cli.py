import asyncio
from pathlib import Path

import typer
from git import InvalidGitRepositoryError, Repo
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from forge.config import ForgeSettings, forge_directory, instructions_path
from forge.context import (
    CheckpointManager,
    Workspace,
    WorkspaceError,
    WriteProposal,
)
from forge.execution import CommandError, CommandProposal, CommandRunner
from forge.agent import (
    AgentService,
    AgentTaskRequest,
    ExecutionPolicy,
    ExecutionPolicyConfig,
    load_execution_policy_config,
)
from forge.providers import ProviderError, get_provider
from forge.providers.models import ChatMessage
from forge.sessions import SessionNotFoundError, SessionStore
from forge.intelligence.context import TokenBudgetBuilder
from forge.intelligence.indexer import RepositoryIndexer
from forge.intelligence.parsers.registry import get_parser_for_language
from forge.intelligence.retrieval import ContextRetrievalEngine
from forge.intelligence.scanner import RepoScanner, detect_language, is_test_file
from forge.intelligence.store import IndexStore
from forge.research import (
    FetcherError,
    ResearchQuery,
    ResearchService,
)
from forge.research.providers.base import SearchError

from typer.core import TyperGroup

class ResearchTyperGroup(TyperGroup):
    def parse_args(self, ctx, args):
        if args and args[0] not in self.commands and not args[0].startswith("-") and args[0] not in {"--help", "-h"}:
            args = ["query"] + args
        return super().parse_args(ctx, args)

app = typer.Typer(
    name="forge",
    help="A model-agnostic AI coding agent for the terminal.",
    no_args_is_help=False,
)
sessions_app = typer.Typer(help="Manage persistent workspace sessions.")
research_app = typer.Typer(cls=ResearchTyperGroup, help="Web research, documentation retrieval, and evidence grounding.")
app.add_typer(sessions_app, name="sessions")
app.add_typer(research_app, name="research")
console = Console()


def resolve_workspace(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise typer.BadParameter(f"Workspace does not exist: {resolved}")
    if not resolved.is_dir():
        raise typer.BadParameter(f"Workspace is not a directory: {resolved}")
    return resolved


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    prompt: str | None = typer.Option(None, "-p", "--prompt", help="Ask Forge one question."),
    model: str | None = typer.Option(None, "--model", help="Select a model provider."),
    plan: bool = typer.Option(False, "--plan", help="Start in planning mode."),
    continue_session: bool = typer.Option(False, "--continue", help="Resume the previous session."),
    agent: bool = typer.Option(False, "--agent", help="Allow a bounded tool loop with per-action approval."),
    policy: str | None = typer.Option(None, "--policy", help="Execution policy: plan, normal, or autonomous."),
    allow_tool: list[str] | None = typer.Option(None, "--allow-tool", help="Allow specific tool autonomously without interactive prompt."),
    rollback: str | None = typer.Option(None, "--rollback", help="Rollback changes made in a specific task/checkpoint ID."),
) -> None:
    if ctx.invoked_subcommand is not None:
        return

    workspace = Path(".")
    root = resolve_workspace(workspace)

    if rollback is not None:
        mgr = CheckpointManager(root)
        target_id = rollback.strip()
        if not target_id:
            cps = mgr.list_checkpoints()
            if not cps:
                console.print("[yellow]No checkpoints recorded to rollback.[/]")
                return
            target_id = cps[0].task_id
        res = mgr.rollback(target_id)
        if res.restored:
            console.print(f"[green]Successfully restored {len(res.restored)} file(s):[/] {', '.join(res.restored)}")
        if res.conflicts:
            console.print(f"[red]Conflicting external changes detected for {len(res.conflicts)} file(s) (not overwritten):[/] {', '.join(res.conflicts)}")
        if not res.restored and not res.conflicts:
            console.print(f"[yellow]No files needed restoration for checkpoint {target_id}.[/]")
        return

    settings = ForgeSettings()
    if model:
        settings.default_model = model

    exec_policy: ExecutionPolicy = ExecutionPolicy.NORMAL
    if policy is not None:
        try:
            exec_policy = ExecutionPolicy(policy.lower())
        except ValueError:
            console.print(f"[red]Invalid policy:[/] {policy}. Must be one of: plan, normal, autonomous")
            raise typer.BadParameter(
                f"Invalid policy '{policy}'. Must be one of: plan, normal, autonomous"
            )
    elif plan:
        exec_policy = ExecutionPolicy.PLAN

    policy_config = load_execution_policy_config(
        workspace_root=root,
        cli_policy=exec_policy if (policy is not None or plan) else None,
        cli_allowed_tools=allow_tool,
    )

    if prompt:
        console.print(
            Panel(
                f"[bold]Model:[/] {settings.default_model}\n"
                f"[bold]Workspace:[/] {root}\n\n"
                f"{prompt}",
                title="Forge",
                border_style="cyan",
            )
        )
        model_name = {
            "ollama": "qwen2.5-coder:7b",
            "gemini": "gemini-2.5-pro",
            "openrouter": "qwen/qwen3-coder",
        }.get(settings.default_model, settings.default_model)
        try:
            provider = get_provider(settings.default_model, settings)
            task_messages = [ChatMessage(role="user", content=prompt)]
            store = SessionStore(root)

            session = None
            if continue_session:
                sessions = store.list(limit=100)
                if sessions:
                    session = store.get(sessions[0].id)
                    task_messages = [
                        ChatMessage(role=turn.role, content=turn.content)
                        for turn in session.turns
                    ] + task_messages
                    if len(task_messages) > 32:
                        task_messages = task_messages[-32:]
                    session_id = sessions[0].id
                else:
                    session = None
                    session_id = None
            else:
                session_id = None

            if session_id is None:
                summary = store.create(prompt[:120])
                session_id = summary.id

            task = AgentTaskRequest(
                model=model_name,
                messages=task_messages,
                max_tokens=1024,
            )
            agent_service = AgentService(root, provider)
            if agent:
                def approve_write(proposal: WriteProposal) -> bool:
                    console.print(
                        Panel(
                            Text(proposal.diff or "No changes to apply."),
                            title=f"Proposed change: {proposal.path}",
                            border_style="yellow",
                        )
                    )
                    return typer.confirm("Apply this exact change?", default=False)

                def approve_command(proposal: CommandProposal) -> bool:
                    console.print("[yellow]Command permission required[/]")
                    console.print(Text(f"Executable: {proposal.executable_path}", overflow="fold"))
                    console.print("Arguments:")
                    for index, argument in enumerate(proposal.argv):
                        console.print(Text(f"  [{index}] {argument!r}", overflow="fold"))
                    console.print(Text(f"Working directory: {proposal.working_directory}", overflow="fold"))
                    console.print(f"Timeout: {proposal.timeout_seconds:g}s")
                    return typer.confirm("Run this exact command?", default=False)

                result = asyncio.run(
                    agent_service.run_autonomous(
                        task,
                        approve_write=approve_write,
                        approve_command=approve_command,
                        policy=policy_config.policy,
                        policy_config=policy_config,
                    )
                )
            else:
                result = asyncio.run(agent_service.run(task))
            store.append_turns(
                session_id,
                [ChatMessage(role="user", content=prompt)],
            )
            store.append_turns(
                session_id,
                [ChatMessage(role="assistant", content=result.generation.content)],
            )
            store.append_outcome(
                session_id,
                "generation",
                {"model": result.generation.model, "done": result.generation.done},
            )
            for outcome in result.tool_outcomes:
                store.append_outcome(
                    session_id,
                    "agent_tool",
                    {
                        "name": outcome.name,
                        "ok": outcome.ok,
                    },
                )
            console.print(result.generation.content, markup=False)
            console.print(f"[dim]Session: {session_id}[/]")
        except (ProviderError, WorkspaceError, CommandError, SessionNotFoundError, ValueError) as error:
            message = getattr(error, "public_message", str(error))
            console.print(f"[red]Could not run prompt:[/] {message}")
            raise typer.Exit(code=1) from error
        return

    if continue_session:
        console.print("[yellow]No saved session is available yet.[/] ")
        return

    console.print(
        Panel.fit(
            "[bold cyan]Forge[/]\n"
            "Repository-aware coding agent\n\n"
            "No provider is configured by default. "
            "Run [bold]forge models[/] to inspect available providers.",
            border_style="cyan",
        )
    )
    mode = policy.lower() if policy else ("plan" if plan else "normal")
    console.print(f"[dim]Workspace:[/] {root}")
    console.print(f"[dim]Mode:[/] {mode}")
    if plan:
        console.print("[yellow]Planning mode is enabled; no files will be modified.[/] ")


@app.command()
def init(workspace: Path = typer.Argument(Path("."), help="Workspace to initialize.")) -> None:
    root = resolve_workspace(workspace)
    forge_dir = forge_directory(root)
    instruction_file = instructions_path(root)
    forge_dir.mkdir(parents=True, exist_ok=True)

    created = False
    if not instruction_file.exists():
        instruction_file.write_text(
            "# Forge Project Instructions\n\n"
            "## Overview\n"
            "Describe this project, its architecture, and its development conventions.\n\n"
            "## Commands\n"
            "- Tests: \n"
            "- Lint: \n"
            "- Build: \n",
            encoding="utf-8",
        )
        created = True

    console.print(f"[green]Initialized Forge workspace[/] at {root}")
    if created:
        console.print(f"Created [bold]{instruction_file}[/]")
    console.print(f"Runtime directory: [bold]{forge_dir}[/]")


def _session_store(workspace: Path) -> SessionStore:
    return SessionStore(resolve_workspace(workspace))


@sessions_app.command("create")
def create_session(
    title: str = typer.Option("New session", "--title", help="Session title."),
    workspace: Path = typer.Option(Path("."), "--workspace", help="Workspace root."),
) -> None:
    try:
        session = _session_store(workspace).create(title)
    except ValueError as error:
        console.print(f"[red]Could not create session:[/] {error}")
        raise typer.Exit(code=1) from error
    console.print(f"[green]Created session[/] {session.id}")


@sessions_app.command("list")
def list_sessions(
    workspace: Path = typer.Option(Path("."), "--workspace", help="Workspace root."),
) -> None:
    sessions = _session_store(workspace).list()
    if not sessions:
        console.print("[dim]No saved sessions.[/]")
        return
    table = Table(title="Forge Sessions")
    table.add_column("ID")
    table.add_column("Title")
    table.add_column("Turns")
    table.add_column("Updated")
    for session in sessions:
        table.add_row(session.id, session.title, str(session.turn_count), session.updated_at)
    console.print(table)


@sessions_app.command("show")
def show_session(
    session_id: str = typer.Argument(help="Session ID."),
    workspace: Path = typer.Option(Path("."), "--workspace", help="Workspace root."),
) -> None:
    try:
        session = _session_store(workspace).get(session_id)
    except SessionNotFoundError as error:
        console.print("[red]Session was not found.[/]")
        raise typer.Exit(code=1) from error
    console.print(f"[bold]{session.summary.title}[/] ({session.summary.id})")
    for turn in session.turns:
        console.print(Text(f"{turn.role}: {turn.content}"))
    if not session.turns:
        console.print("[dim]No conversation turns.[/]")


@sessions_app.command("delete")
def delete_session(
    session_id: str = typer.Argument(help="Session ID."),
    workspace: Path = typer.Option(Path("."), "--workspace", help="Workspace root."),
) -> None:
    if not typer.confirm(f"Delete session {session_id}?", default=False):
        console.print("[dim]Session was not deleted.[/]")
        raise typer.Exit(code=1)
    try:
        _session_store(workspace).delete(session_id)
    except SessionNotFoundError as error:
        console.print("[red]Session was not found.[/]")
        raise typer.Exit(code=1) from error
    console.print("[green]Session deleted.[/]")


@app.command()
def models() -> None:
    settings = ForgeSettings()
    table = Table(title="Model Providers")
    table.add_column("Provider")
    table.add_column("Execution")
    table.add_column("Status")

    ollama_models: list[str] = []
    try:
        ollama_models = asyncio.run(get_provider("ollama", settings).list_models())
        ollama_status = "Available" if ollama_models else "No models installed"
    except ProviderError as error:
        ollama_status = "Unavailable"
        console.print(f"[yellow]{error.public_message}[/]")

    table.add_row("Ollama", "Local", ollama_status)
    for provider_name, label, configured in (
        ("gemini", "Gemini", settings.gemini_api_key is not None),
        ("openrouter", "OpenRouter", settings.openrouter_api_key is not None),
    ):
        status = "Not configured"
        if configured:
            try:
                available_models = asyncio.run(
                    get_provider(provider_name, settings).list_models()
                )
                status = "Available" if available_models else "No models available"
            except ProviderError as error:
                status = "Unavailable"
                console.print(f"[yellow]{label}: {error.public_message}[/]")
        table.add_row(label, "Cloud API", status)
    console.print(table)
    if ollama_models:
        console.print(f"Ollama models: {', '.join(ollama_models)}")


@app.command("files")
def list_files(
    workspace: Path = typer.Argument(Path("."), help="Workspace to inspect."),
    pattern: str = typer.Option("**/*", "--pattern", help="Relative glob pattern."),
    limit: int = typer.Option(200, min=1, max=1000, help="Maximum files to list."),
) -> None:
    root = resolve_workspace(workspace)
    try:
        result = Workspace(root).list_files(pattern, limit=limit)
    except (WorkspaceError, ValueError) as error:
        console.print(f"[red]Could not list workspace files:[/] {error}")
        raise typer.Exit(code=1) from error

    for path in result.paths:
        console.print(path, markup=False)
    if result.truncated:
        console.print("[yellow]File list truncated; narrow the pattern or increase the limit.[/]")
    elif not result.paths:
        console.print("[dim]No readable files matched.[/]")


@app.command("read")
def read_file(
    path: str = typer.Argument(help="Workspace-relative file path."),
    workspace: Path = typer.Option(Path("."), "--workspace", help="Workspace root."),
) -> None:
    root = resolve_workspace(workspace)
    try:
        content = Workspace(root).read_text(path)
    except WorkspaceError as error:
        console.print(f"[red]Could not read workspace file:[/] {error}")
        raise typer.Exit(code=1) from error
    console.print(content, markup=False, end="")


@app.command("write")
def write_file(
    path: str = typer.Argument(help="Workspace-relative file path."),
    content: str = typer.Option(..., "--content", help="Complete replacement file content."),
    workspace: Path = typer.Option(Path("."), "--workspace", help="Workspace root."),
    session_id: str | None = typer.Option(
        None,
        "--session",
        help="Existing session ID to receive approved change history.",
    ),
) -> None:
    root = resolve_workspace(workspace)
    selected_workspace = Workspace(root)
    session_store = None
    if session_id is not None:
        if len(session_id) != 32 or any(character not in "0123456789abcdef" for character in session_id):
            console.print("[red]Workspace write was not applied:[/] Invalid session ID.")
            raise typer.Exit(code=1)
        session_store = SessionStore(root)
        try:
            session_store.get(session_id)
        except SessionNotFoundError:
            console.print("[red]Workspace write was not applied:[/] Session was not found.")
            raise typer.Exit(code=1)

    def request_approval(proposal: WriteProposal) -> bool:
        console.print(
            Panel(
                Text(proposal.diff or "No changes to apply."),
                title=f"Proposed change: {proposal.path}",
                border_style="yellow",
            )
        )
        return typer.confirm("Apply this exact change?", default=False)

    try:
        result = selected_workspace.write_text(path, content, approve=request_approval)
    except WorkspaceError as error:
        console.print(f"[red]Workspace write was not applied:[/] {error}")
        raise typer.Exit(code=1) from error

    if result.changed:
        if session_store is not None:
            session_store.append_outcome(
                session_id,
                "workspace_write",
                {
                    "path": result.path,
                    "bytes_written": result.bytes_written,
                    "previous_sha256": result.previous_sha256,
                    "current_sha256": result.current_sha256,
                },
            )
        console.print(f"[green]Updated {result.path}[/] ({result.bytes_written} bytes)")
    else:
        console.print(f"[dim]No changes needed for {result.path}.[/]")


@app.command("search")
def search_files(
    query: str = typer.Argument(help="Text to search for."),
    workspace: Path = typer.Option(Path("."), "--workspace", help="Workspace root."),
    pattern: str = typer.Option("**/*", "--pattern", help="Relative glob pattern."),
    limit: int = typer.Option(100, min=1, max=1000, help="Maximum matching lines."),
    case_sensitive: bool = typer.Option(False, "--case-sensitive", help="Match case exactly."),
) -> None:
    root = resolve_workspace(workspace)
    try:
        result = Workspace(root).search_text(
            query,
            pattern=pattern,
            limit=limit,
            case_sensitive=case_sensitive,
        )
    except (WorkspaceError, ValueError) as error:
        console.print(f"[red]Could not search workspace:[/] {error}")
        raise typer.Exit(code=1) from error

    for match in result.matches:
        line = f"{match.line}…" if match.line_truncated else match.line
        console.print(f"{match.path}:{match.line_number}: {line}", markup=False)
    if result.files_skipped:
        console.print(f"[yellow]Skipped {result.files_skipped} unreadable or oversized file(s).[/]")
    if result.truncated:
        console.print("[yellow]Search results truncated; narrow the pattern or increase the limit.[/]")
    elif not result.matches:
        console.print("[dim]No matches found.[/]")


@app.command("run")
def run_command(
    command: list[str] = typer.Argument(
        ..., help="Approved development command and arguments."
    ),
    workspace: Path = typer.Option(Path("."), "--workspace", help="Workspace root."),
    cwd: str = typer.Option(".", "--cwd", help="Workspace-relative working directory."),
    timeout: float = typer.Option(
        30.0, min=0.1, max=300.0, help="Command timeout in seconds."
    ),
) -> None:
    root = resolve_workspace(workspace)
    runner = CommandRunner(root)

    def request_approval(proposal: CommandProposal) -> bool:
        console.print(
            "[yellow]Command permission required[/]",
        )
        console.print(
            Text(f"Executable: {proposal.executable_path}", overflow="fold")
        )
        console.print("Arguments:")
        for index, argument in enumerate(proposal.argv):
            console.print(Text(f"  [{index}] {argument!r}", overflow="fold"))
        console.print(
            Text(f"Working directory: {proposal.working_directory}", overflow="fold")
        )
        console.print(
            f"Timeout: {proposal.timeout_seconds:g}s",
        )
        return typer.confirm("Run this exact command?", default=False)

    try:
        result = asyncio.run(
            runner.run(
                command,
                approve=request_approval,
                cwd=cwd,
                timeout_seconds=timeout,
            )
        )
    except CommandError as error:
        console.print(f"[red]Command was not run:[/] {error}")
        raise typer.Exit(code=1) from error

    if result.stdout:
        console.print(Text(result.stdout), end="")
    if result.stderr:
        console.print(Text(result.stderr, style="red"), end="")
    if result.stdout_truncated or result.stderr_truncated:
        console.print(
            "[yellow]Command output was truncated at the configured limit.[/]"
        )
    if result.timed_out:
        console.print("[red]Command exceeded its timeout and was terminated.[/]")
        raise typer.Exit(code=124)
    raise typer.Exit(code=result.exit_code if result.exit_code is not None else 1)


@app.command()
def diff(workspace: Path = typer.Argument(Path("."), help="Repository to inspect.")) -> None:
    root = resolve_workspace(workspace)
    try:
        repo = Repo(root, search_parent_directories=True)
    except InvalidGitRepositoryError:
        console.print(f"[red]Not a Git repository:[/] {root}")
        raise typer.Exit(code=1)

    output = repo.git.diff()
    if output:
        console.print(output)
    else:
        console.print("[dim]No unstaged changes.[/] ")


@app.command("index")
def index_command(
    status: bool = typer.Option(False, "--status", help="Show index status and statistics."),
    force: bool = typer.Option(False, "--force", help="Force a clean re-indexing run from scratch."),
    workspace: Path = typer.Option(Path("."), "-w", "--workspace", help="Path to workspace root."),
) -> None:
    root = resolve_workspace(workspace)
    store = IndexStore(root)

    if status:
        stats = store.get_stats()
        if not stats.last_run_at:
            console.print("[dim]No repository index found. Run 'forge index' to index the workspace.[/]")
            return

        table = Table(title="Repository Intelligence Index Status")
        table.add_column("Property", style="cyan")
        table.add_column("Value", style="bold green")

        table.add_row("Total Files Indexed", str(stats.total_files))
        table.add_row("Total Symbols Indexed", str(stats.total_symbols))
        table.add_row("Last Run Status", str(stats.last_run_status))
        table.add_row("Last Run Completed", str(stats.last_run_at))
        table.add_row("Last Run Duration", f"{stats.last_run_duration_ms:0.1f} ms")

        if stats.languages:
            langs_str = ", ".join(f"{k}: {v}" for k, v in stats.languages.items())
            table.add_row("Languages", langs_str)

        if stats.symbols_by_kind:
            kinds_str = ", ".join(f"{k}: {v}" for k, v in stats.symbols_by_kind.items())
            table.add_row("Symbols Breakdown", kinds_str)

        console.print(table)
        return

    indexer = RepositoryIndexer(root, store=store)
    try:
        console.print(f"[cyan]Indexing workspace:[/] {root} {'(forced full rebuild)' if force else '(incremental)'}...")
        stats = indexer.index(force=force)
        console.print(
            f"[green]Indexed repository successfully[/] in {stats.last_run_duration_ms:0.1f}ms: "
            f"[bold]{stats.total_files}[/] files, [bold]{stats.total_symbols}[/] symbols."
        )
    except Exception as error:
        console.print(f"[red]Indexing failed:[/] {error}")
        raise typer.Exit(code=1) from error


@app.command("map")
def map_command(
    workspace: Path = typer.Option(Path("."), "-w", "--workspace", help="Path to workspace root."),
) -> None:
    root = resolve_workspace(workspace)
    scanner = RepoScanner(root)
    inventory = scanner.scan()
    repo_map = scanner.generate_repo_map(inventory)

    console.print(Panel(repo_map.summary_text, title="Repository Intelligence Map", border_style="cyan"))
    console.print(repo_map.tree_text)


@app.command("symbols")
def symbols_command(
    query: str = typer.Argument(..., help="Symbol query to search for."),
    pattern: str = typer.Option("**/*", "--pattern", help="Glob pattern to filter code files."),
    limit: int = typer.Option(50, "--limit", help="Maximum number of matches."),
    workspace: Path = typer.Option(Path("."), "-w", "--workspace", help="Path to workspace root."),
) -> None:
    root = resolve_workspace(workspace)
    store = IndexStore(root)
    stats = store.get_stats()

    # If SQLite index has symbols, use persistent index
    if stats.total_symbols > 0:
        results = store.search_symbols(query, limit=limit)
        if not results:
            console.print("[dim]No matching symbols found in persistent index.[/]")
            return
        table = Table(title=f"Indexed Symbols matching '{query}'")
        table.add_column("Kind", style="cyan")
        table.add_column("Name", style="bold green")
        table.add_column("Location", style="yellow")
        table.add_column("Signature / Details", style="dim")
        for m in results:
            details = m.signature or (m.docstring[:60] if m.docstring else "")
            table.add_row(m.kind, m.name, f"{m.path}:{m.line_number}", details)
        console.print(table)
        return

    # Fallback to workspace regex symbol search
    ws = Workspace(root)
    try:
        ws_results = ws.search_symbols(query, pattern=pattern, limit=limit)
        if not ws_results.matches:
            console.print("[dim]No matching symbols found.[/]")
            return
        table = Table(title=f"Symbols matching '{query}'")
        table.add_column("Kind", style="cyan")
        table.add_column("Name", style="bold green")
        table.add_column("Location", style="yellow")
        table.add_column("Line", style="dim")
        for wm in ws_results.matches:
            table.add_row(wm.kind, wm.name, f"{wm.path}:{wm.line_number}", wm.line)
        console.print(table)
    except WorkspaceError as error:
        console.print(f"[red]Symbol search error:[/] {getattr(error, 'public_message', str(error))}")
        raise typer.Exit(code=1) from error


@app.command("references")
def references_command(
    symbol: str = typer.Argument(..., help="Symbol name to find references for."),
    limit: int = typer.Option(100, "--limit", help="Maximum number of references."),
    workspace: Path = typer.Option(Path("."), "-w", "--workspace", help="Path to workspace root."),
) -> None:
    root = resolve_workspace(workspace)
    store = IndexStore(root)
    stats = store.get_stats()

    if stats.total_symbols == 0:
        console.print("[yellow]Repository is not yet indexed. Indexing now...[/]")
        indexer = RepositoryIndexer(root, store=store)
        indexer.index()

    refs = store.find_references(symbol, limit=limit)
    if not refs:
        console.print(f"[dim]No references found for symbol '{symbol}'.[/]")
        return

    table = Table(title=f"References to '{symbol}'")
    table.add_column("Symbol", style="bold green")
    table.add_column("Location", style="yellow")
    table.add_column("Kind", style="cyan")
    table.add_column("Status", style="magenta")
    table.add_column("Target", style="dim")

    for r in refs:
        status = "[green]Resolved[/]" if r.get("is_resolved") else "[yellow]Unresolved[/]"
        target = r.get("target_path") or (r.get("target_symbol_name") or "-")
        table.add_row(
            r["symbol_name"],
            f"{r['source_path']}:{r['line_number']}",
            r["kind"],
            status,
            str(target),
        )
    console.print(table)


@app.command("explain")
def explain_command(
    path: str = typer.Argument(..., help="Workspace-relative file path to explain."),
    workspace: Path = typer.Option(Path("."), "-w", "--workspace", help="Path to workspace root."),
) -> None:
    root = resolve_workspace(workspace)
    ws = Workspace(root)

    try:
        content = ws.read_text(path)
    except WorkspaceError as error:
        console.print(f"[red]Could not read file:[/] {error}")
        raise typer.Exit(code=1) from error

    lang = detect_language(path)
    parser = get_parser_for_language(lang)

    extracted = parser.parse(path, content) if parser else None
    line_count = len(content.splitlines())
    byte_count = len(content.encode("utf-8"))

    console.print(
        Panel(
            f"[bold]Path:[/] {path}\n"
            f"[bold]Language:[/] {lang} ({line_count} lines, {byte_count:,} bytes)\n"
            f"[bold]Is Test File:[/] {'Yes' if is_test_file(path) else 'No'}",
            title=f"Explain File: {path}",
            border_style="cyan",
        )
    )

    if extracted and extracted.symbols:
        table = Table(title="Defined Symbols")
        table.add_column("Kind", style="cyan")
        table.add_column("Name", style="bold green")
        table.add_column("Line", style="yellow")
        table.add_column("Parent", style="dim")
        table.add_column("Signature / Doc", style="dim")
        for s in extracted.symbols:
            sig = s.signature or (s.docstring[:60] if s.docstring else "")
            table.add_row(s.kind.value if hasattr(s.kind, "value") else str(s.kind), s.name, str(s.line_number), s.parent_name or "-", sig)
        console.print(table)

    if extracted and extracted.imports:
        table_imp = Table(title="Imports")
        table_imp.add_column("Module", style="green")
        table_imp.add_column("Imported Names", style="cyan")
        table_imp.add_column("Line", style="dim")
        for imp in extracted.imports:
            table_imp.add_row(imp.module, ", ".join(imp.imported_names), str(imp.line_number))
        console.print(table_imp)

    # Check incoming references from index
    store = IndexStore(root)
    with store._session() as conn:
        incoming_rows = conn.execute(
            """
            SELECT references_index.symbol_name, references_index.line_number, files.relative_path AS caller
            FROM references_index
            JOIN files ON files.id = references_index.file_id
            WHERE references_index.target_path = ? AND files.is_deleted = 0
            LIMIT 20
            """,
            (path,),
        ).fetchall()
        if incoming_rows:
            table_inc = Table(title="Incoming References")
            table_inc.add_column("Caller File", style="yellow")
            table_inc.add_column("Line", style="dim")
            table_inc.add_column("Symbol Called", style="green")
            for inc in incoming_rows:
                table_inc.add_row(inc["caller"], str(inc["line_number"]), inc["symbol_name"])
            console.print(table_inc)


@app.command("context")
def context_command(
    task: str = typer.Argument(..., help="Task description to retrieve context for."),
    budget: int = typer.Option(4000, "--budget", help="Maximum token budget."),
    workspace: Path = typer.Option(Path("."), "-w", "--workspace", help="Path to workspace root."),
) -> None:
    root = resolve_workspace(workspace)
    retrieval = ContextRetrievalEngine(root)
    retrieved = retrieval.retrieve(task)

    builder = TokenBudgetBuilder(max_tokens=budget)
    built = builder.build_context(retrieved)

    # Manifest Table
    table = Table(title="Context Retrieval Manifest")
    table.add_column("Item / Path", style="bold green")
    table.add_column("Est. Tokens", style="cyan")
    table.add_column("Score", style="yellow")
    table.add_column("Relevance Reasons", style="dim")

    for item in built.manifest.included_items:
        table.add_row(item.path, str(item.tokens), f"{item.score:g}" if item.score else "-", "; ".join(item.reasons))
    console.print(table)

    if built.manifest.excluded_items:
        table_exc = Table(title="Excluded Candidates")
        table_exc.add_column("Path", style="dim")
        table_exc.add_column("Est. Tokens", style="dim")
        table_exc.add_column("Reason", style="red")
        for exc in built.manifest.excluded_items:
            table_exc.add_row(exc.path, str(exc.tokens), "; ".join(exc.reasons))
        console.print(table_exc)

    console.print(
        Panel(
            f"[bold]Task:[/] {task}\n"
            f"[bold]Total Estimated Tokens:[/] {built.manifest.used_tokens} / {built.manifest.budget_tokens}\n"
            f"[bold]Included Items:[/] {len(built.manifest.included_items)} | [bold]Excluded:[/] {len(built.manifest.excluded_items)}",
            title="Context Budget Summary",
            border_style="green",
        )
    )


@app.command("checkpoints")
def checkpoints_command(
    workspace: Path = typer.Option(Path("."), "-w", "--workspace", help="Path to workspace root."),
) -> None:
    root = resolve_workspace(workspace)
    mgr = CheckpointManager(root)
    cps = mgr.list_checkpoints()
    if not cps:
        console.print("[dim]No checkpoints recorded in this workspace.[/]")
        return
    table = Table(title="Workspace Checkpoints")
    table.add_column("Task ID", style="bold cyan")
    table.add_column("Files Modified", style="green")
    for cp in cps:
        table.add_row(cp.task_id, ", ".join(cp.snapshots.keys()))
    console.print(table)


@app.command("rollback")
def rollback_command(
    task_id: str | None = typer.Argument(None, help="Task ID to rollback. If omitted, rolls back the most recent checkpoint."),
    workspace: Path = typer.Option(Path("."), "-w", "--workspace", help="Path to workspace root."),
) -> None:
    root = resolve_workspace(workspace)
    mgr = CheckpointManager(root)
    target_id = task_id
    if not target_id:
        cps = mgr.list_checkpoints()
        if not cps:
            console.print("[yellow]No checkpoints recorded to rollback.[/]")
            return
        target_id = cps[0].task_id
    res = mgr.rollback(target_id)
    if res.restored:
        console.print(f"[green]Successfully restored {len(res.restored)} file(s):[/] {', '.join(res.restored)}")
    if res.conflicts:
        console.print(f"[red]Conflicting external changes detected for {len(res.conflicts)} file(s) (not overwritten):[/] {', '.join(res.conflicts)}")
    if not res.restored and not res.conflicts:
        console.print(f"[yellow]No files needed restoration for checkpoint {target_id}.[/]")


@research_app.command("query", hidden=True)
def research_query_command(
    query: str = typer.Argument(..., help="Technical search query for web and documentation research."),
    domain: list[str] | None = typer.Option(None, "--domain", "-d", help="Restrict research to specific domain(s)."),
    limit: int = typer.Option(5, "--limit", "-l", help="Maximum number of sources to retrieve."),
    recency: str | None = typer.Option(None, "--recency", help="Filter by recency (e.g. day, week, month, year)."),
    package: str | None = typer.Option(None, "--package", help="Optional package/framework name."),
    version: str | None = typer.Option(None, "--version", help="Optional version filter."),
    workspace: Path = typer.Option(Path("."), "-w", "--workspace", help="Path to workspace root."),
) -> None:
    if not query or not query.strip():
        console.print("[yellow]Please provide a research query or run 'forge research --help'.[/]")
        raise typer.Exit(code=1)

    root = resolve_workspace(workspace)
    service = ResearchService(root)
    research_query = ResearchQuery(
        query=query.strip(),
        limit=limit,
        domain_restrictions=domain or [],
        recency=recency,
        package=package,
        version=version,
    )

    console.print(
        Panel(
            f"[bold]Query:[/] {query}\n"
            f"[bold]Workspace:[/] {root}\n"
            f"[bold]Limit:[/] {limit}"
            + (f" | [bold]Domain(s):[/] {', '.join(domain)}" if domain else "")
            + (f" | [bold]Version:[/] {version}" if version else ""),
            title="Forge Research",
            border_style="cyan",
        )
    )

    try:
        report = asyncio.run(service.research(research_query))
    except (SearchError, FetcherError, ValueError) as exc:
        msg = getattr(exc, "public_message", str(exc))
        console.print(f"[red]Research failed:[/] {msg}")
        raise typer.Exit(code=1) from exc

    # Sources table
    table = Table(title="Retrieved & Ranked Sources")
    table.add_column("ID", style="bold cyan")
    table.add_column("Title", style="bold")
    table.add_column("Domain", style="green")
    table.add_column("Score", style="yellow")
    table.add_column("Status", style="dim")

    for src in report.sources:
        status_style = "green" if src.retrieval_status == "success" else "red"
        table.add_row(
            src.source_id,
            src.page_title[:45],
            src.domain,
            f"{src.relevance_score:.1f}",
            f"[{status_style}]{src.retrieval_status}[/{status_style}]",
        )
    console.print(table)

    if report.verified_facts:
        facts_panel = Panel(
            "\n".join(f"• {f}" for f in report.verified_facts),
            title="Verified Evidence Facts",
            border_style="green",
        )
        console.print(facts_panel)

    if report.conflicting_sources:
        conflicts_text = "\n".join(f"• {c.get('issue')}" for c in report.conflicting_sources)
        console.print(Panel(conflicts_text, title="Identified Conflicts", border_style="yellow"))

    console.print(f"[dim]Research ID: {report.research_id}[/]")


@research_app.command("fetch")
def research_fetch_command(
    url: str = typer.Argument(..., help="URL of documentation page to retrieve safely."),
    workspace: Path = typer.Option(Path("."), "-w", "--workspace", help="Path to workspace root."),
) -> None:
    root = resolve_workspace(workspace)
    service = ResearchService(root)

    console.print(f"[dim]Safely retrieving:[/] {url}")
    try:
        doc = asyncio.run(service.fetch_url(url))
    except (FetcherError, ValueError) as exc:
        msg = getattr(exc, "public_message", str(exc))
        console.print(f"[red]Fetch failed:[/] {msg}")
        raise typer.Exit(code=1) from exc

    table = Table(title="Retrieved Document Metadata")
    table.add_column("Property", style="bold cyan")
    table.add_column("Value", style="green")
    table.add_row("Title", doc.title)
    table.add_row("Canonical URL", doc.canonical_url)
    table.add_row("Domain", doc.domain)
    table.add_row("Content-Type", doc.content_type)
    table.add_row("Content Hash", doc.content_hash[:16] + "..." if doc.content_hash else "-")
    table.add_row("Status", doc.status)
    table.add_row("Headings Count", str(len(doc.headings)))
    table.add_row("Code Blocks Count", str(len(doc.code_blocks)))
    console.print(table)

    if doc.extracted_text:
        preview = doc.extracted_text[:600]
        console.print(Panel(preview, title="Extracted Content Preview", border_style="dim"))


@research_app.command("show")
def research_show_command(
    research_id: str = typer.Argument(..., help="Research report ID to inspect."),
    workspace: Path = typer.Option(Path("."), "-w", "--workspace", help="Path to workspace root."),
) -> None:
    root = resolve_workspace(workspace)
    service = ResearchService(root)
    report = service.get_report(research_id)
    if report is None:
        console.print(f"[red]Research report not found:[/] {research_id}")
        raise typer.Exit(code=1)

    console.print(
        Panel(
            f"[bold]Query:[/] {report.query.query}\n"
            f"[bold]Status:[/] {report.status.value}\n"
            f"[bold]Created:[/] {report.created_at}\n"
            f"[bold]Summary:[/] {report.summary}",
            title=f"Research Report: {report.research_id}",
            border_style="cyan",
        )
    )

    if report.citations:
        table = Table(title="Citations")
        table.add_column("Citation ID", style="cyan")
        table.add_column("Source", style="green")
        table.add_column("Section", style="dim")
        table.add_column("Excerpt", style="white")
        for cit in report.citations:
            table.add_row(
                cit.citation_id,
                f"{cit.source_title} ({cit.source_id})",
                cit.section_heading or "-",
                cit.relevant_excerpt[:100] + "...",
            )
        console.print(table)


@research_app.command("sources")
def research_sources_command(
    research_id: str = typer.Argument(..., help="Research report ID to list sources for."),
    workspace: Path = typer.Option(Path("."), "-w", "--workspace", help="Path to workspace root."),
) -> None:
    root = resolve_workspace(workspace)
    service = ResearchService(root)
    report = service.get_report(research_id)
    if report is None:
        console.print(f"[red]Research report not found:[/] {research_id}")
        raise typer.Exit(code=1)

    table = Table(title=f"Sources for Research: {research_id}")
    table.add_column("ID", style="bold cyan")
    table.add_column("Title", style="bold")
    table.add_column("URL", style="green")
    table.add_column("Type", style="dim")
    table.add_column("Score", style="yellow")
    for src in report.sources:
        table.add_row(
            src.source_id,
            src.page_title[:40],
            src.canonical_url,
            src.source_type,
            f"{src.relevance_score:.1f}",
        )
    console.print(table)


@research_app.command("cache")
def research_cache_command(
    status: bool = typer.Option(False, "--status", help="Show research cache status."),
    clear: bool = typer.Option(False, "--clear", help="Clear all entries from research cache."),
    workspace: Path = typer.Option(Path("."), "-w", "--workspace", help="Path to workspace root."),
) -> None:
    root = resolve_workspace(workspace)
    service = ResearchService(root)

    if clear:
        deleted = service.cache.clear()
        console.print(f"[green]Cleared {deleted} document(s) from research cache.[/]")
        return

    # Default show status
    st = service.cache.status()
    table = Table(title="Research Cache Status")
    table.add_column("Metric", style="bold cyan")
    table.add_column("Value", style="green")
    table.add_row("Total Cached Documents", str(st["total_documents"]))
    table.add_row("Total Cache Size", f"{st['total_size_mb']} MB")
    table.add_row("Max Documents Limit", str(st["max_documents_limit"]))
    table.add_row("Max Cache Size Limit", f"{st['max_bytes_limit_mb']} MB")
    table.add_row("Cache TTL", f"{st['ttl_days']} days")
    table.add_row("Expired Documents", str(st["expired_documents"]))
    table.add_row("Oldest Entry", str(st["oldest_entry"] or "-"))
    table.add_row("Newest Entry", str(st["newest_entry"] or "-"))
    console.print(table)
