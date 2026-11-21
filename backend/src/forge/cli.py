import asyncio
from pathlib import Path

import typer
from git import InvalidGitRepositoryError, Repo
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from forge.config import ForgeSettings, forge_directory, instructions_path
from forge.context import Workspace, WorkspaceError, WriteProposal
from forge.execution import CommandError, CommandProposal, CommandRunner
from forge.providers import ProviderError, get_provider

app = typer.Typer(
    name="forge",
    help="A model-agnostic AI coding agent for the terminal.",
    no_args_is_help=False,
)
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
) -> None:
    if ctx.invoked_subcommand is not None:
        return

    workspace = Path(".")
    root = resolve_workspace(workspace)
    settings = ForgeSettings()
    if model:
        settings.default_model = model

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
        console.print(
            "[yellow]The prompt runner is not implemented yet.[/] "
            "Use [bold]forge init[/] to create project instructions."
        )
        raise typer.Exit(code=2)

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
    mode = "plan" if plan else "normal"
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
    table.add_row(
        "Gemini",
        "Cloud API",
        "Configured (adapter pending)" if settings.gemini_api_key else "Not configured",
    )
    table.add_row(
        "OpenRouter",
        "Cloud API",
        "Configured (adapter pending)" if settings.openrouter_api_key else "Not configured",
    )
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
) -> None:
    root = resolve_workspace(workspace)
    selected_workspace = Workspace(root)

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
