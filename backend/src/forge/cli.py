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

app = typer.Typer(
    name="forge",
    help="A model-agnostic AI coding agent for the terminal.",
    no_args_is_help=False,
)
sessions_app = typer.Typer(help="Manage persistent workspace sessions.")
app.add_typer(sessions_app, name="sessions")
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


@app.command("symbols")
def symbols_command(
    query: str = typer.Argument(..., help="Symbol query to search for."),
    pattern: str = typer.Option("**/*", "--pattern", help="Glob pattern to filter code files."),
    limit: int = typer.Option(50, "--limit", help="Maximum number of matches."),
    workspace: Path = typer.Option(Path("."), "-w", "--workspace", help="Path to workspace root."),
) -> None:
    root = resolve_workspace(workspace)
    ws = Workspace(root)
    try:
        results = ws.search_symbols(query, pattern=pattern, limit=limit)
        if not results.matches:
            console.print("[dim]No matching symbols found.[/]")
            return
        table = Table(title=f"Symbols matching '{query}'")
        table.add_column("Kind", style="cyan")
        table.add_column("Name", style="bold green")
        table.add_column("Location", style="yellow")
        table.add_column("Line", style="dim")
        for m in results.matches:
            table.add_row(m.kind, m.name, f"{m.path}:{m.line_number}", m.line)
        console.print(table)
    except WorkspaceError as error:
        console.print(f"[red]Symbol search error:[/] {getattr(error, 'public_message', str(error))}")
        raise typer.Exit(code=1) from error


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
