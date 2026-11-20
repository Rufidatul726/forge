from pathlib import Path

import httpx
import typer
from git import InvalidGitRepositoryError, Repo
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from forge.config import ForgeSettings, forge_directory, instructions_path

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

    ollama_status = "Unavailable"
    try:
        response = httpx.get(f"{settings.ollama_host}/api/tags", timeout=2.0)
        if response.is_success:
            ollama_status = "Available"
    except httpx.HTTPError:
        pass

    table.add_row("Ollama", "Local", ollama_status)
    table.add_row("Gemini", "Cloud API", "Configured" if settings.gemini_api_key else "Not configured")
    table.add_row(
        "OpenRouter",
        "Cloud API",
        "Configured" if settings.openrouter_api_key else "Not configured",
    )
    console.print(table)


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
