from forge.cli import app
from typer.testing import CliRunner

runner = CliRunner()


def test_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "A model-agnostic" in result.stdout


def test_init_creates_instructions(tmp_path):
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0
    assert (tmp_path / "FORGE.md").exists()
    assert (tmp_path / ".forge").is_dir()


def test_prompt_runs_read_only_agent_task_and_persists_session(tmp_path, monkeypatch):
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")

    class FakeProvider:
        async def generate(self, payload):
            from forge.providers.models import GenerationResult

            assert payload.messages[0].role == "system"
            assert "main.py" in payload.messages[0].content
            return GenerationResult(
                model=payload.model,
                content="The repository has a main.py entry point.",
                done=True,
            )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("forge.cli.get_provider", lambda name, settings: FakeProvider())
    result = runner.invoke(app, ["-p", "Explain this repository"])
    assert result.exit_code == 0
    assert "The repository has a main.py entry point." in result.stdout
    assert "Session:" in result.stdout


def test_diff_outside_repository(tmp_path):
    result = runner.invoke(app, ["diff", str(tmp_path)])
    assert result.exit_code == 1
    assert "Not a Git repository" in result.stdout


def test_models_lists_ollama_models_without_network(monkeypatch):
    class FakeProvider:
        async def list_models(self):
            return ["qwen2.5-coder:7b"]

    monkeypatch.setattr("forge.cli.get_provider", lambda name, settings: FakeProvider())
    result = runner.invoke(app, ["models"])

    assert result.exit_code == 0
    assert "Available" in result.stdout
    assert "qwen2.5-coder:7b" in result.stdout


def test_models_reports_ollama_errors(monkeypatch):
    from forge.providers import ProviderUnavailableError

    def unavailable_provider(name, settings):
        raise ProviderUnavailableError

    monkeypatch.setattr("forge.cli.get_provider", unavailable_provider)
    result = runner.invoke(app, ["models"])

    assert result.exit_code == 0
    assert "configured model provider is unavailable" in result.stdout
    assert "Unavailable" in result.stdout


def test_files_command_lists_workspace_files(tmp_path):
    (tmp_path / "main.py").write_text("print('hello')", encoding="utf-8")
    (tmp_path / ".env").write_text("secret", encoding="utf-8")

    result = runner.invoke(app, ["files", str(tmp_path)])

    assert result.exit_code == 0
    assert "main.py" in result.stdout
    assert ".env" not in result.stdout


def test_read_command_refuses_paths_outside_workspace(tmp_path):
    secret = tmp_path.parent / f"{tmp_path.name}-secret.txt"
    secret.write_text("not for the workspace", encoding="utf-8")

    result = runner.invoke(app, ["read", str(secret), "--workspace", str(tmp_path)])

    assert result.exit_code == 1
    assert "Could not read workspace file" in result.stdout
    assert "not for the workspace" not in result.stdout


def test_write_command_requires_and_honors_confirmation(tmp_path):
    target = tmp_path / "new.py"
    result = runner.invoke(
        app,
        ["write", "new.py", "--content", "print('hello')\n", "--workspace", str(tmp_path)],
        input="y\n",
    )

    assert result.exit_code == 0
    assert "Apply this exact change?" in result.stdout
    assert "Updated new.py" in result.stdout
    assert target.read_text(encoding="utf-8") == "print('hello')\n"


def test_write_command_decline_leaves_workspace_unchanged(tmp_path):
    target = tmp_path / "new.py"
    result = runner.invoke(
        app,
        ["write", "new.py", "--content", "print('hello')\n", "--workspace", str(tmp_path)],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "Workspace write was not approved" in result.stdout
    assert not target.exists()


def test_write_command_rejects_sensitive_targets_without_prompt(tmp_path):
    result = runner.invoke(
        app,
        ["write", ".env.local", "--content", "TOKEN=secret", "--workspace", str(tmp_path)],
        input="y\n",
    )

    assert result.exit_code == 1
    assert "Apply this exact change?" not in result.stdout
    assert not (tmp_path / ".env.local").exists()


def test_search_command_reports_workspace_matches(tmp_path):
    (tmp_path / "main.py").write_text("forge is ready\n", encoding="utf-8")
    (tmp_path / ".env").write_text("forge secret\n", encoding="utf-8")

    result = runner.invoke(app, ["search", "forge", "--workspace", str(tmp_path)])

    assert result.exit_code == 0
    assert "main.py:1: forge is ready" in result.stdout
    assert "forge secret" not in result.stdout


def test_run_command_shows_exact_proposal_and_requires_approval(tmp_path):
    marker = tmp_path / "ran.txt"
    code = f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"

    result = runner.invoke(
        app,
        ["run", "--workspace", str(tmp_path), "--", "python", "-c", code],
        input="y\n",
    )

    assert result.exit_code == 0
    assert "Command permission required" in result.stdout
    assert "Run this exact command?" in result.stdout
    assert repr(code) in result.stdout.replace("\n", "")
    assert marker.read_text(encoding="utf-8") == "ran"


def test_run_command_defaults_to_denial_and_preserves_nonzero_exit(tmp_path):
    marker = tmp_path / "must-not-exist.txt"
    code = f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"
    declined = runner.invoke(
        app,
        ["run", "--workspace", str(tmp_path), "--", "python", "-c", code],
        input="n\n",
    )

    assert declined.exit_code == 1
    assert "Command was not run" in declined.stdout
    assert not marker.exists()

    nonzero = runner.invoke(
        app,
        [
            "run",
            "--workspace",
            str(tmp_path),
            "--",
            "python",
            "-c",
            "raise SystemExit(7)",
        ],
        input="y\n",
    )

    assert nonzero.exit_code == 7


def test_run_command_reports_timeout_with_timeout_exit_code(tmp_path):
    result = runner.invoke(
        app,
        [
            "run",
            "--workspace",
            str(tmp_path),
            "--timeout",
            "0.1",
            "--",
            "python",
            "-c",
            "import time; time.sleep(10)",
        ],
        input="y\n",
    )

    assert result.exit_code == 124
    assert "exceeded its timeout" in result.stdout


def test_session_commands_share_the_workspace_store(tmp_path):
    created = runner.invoke(
        app,
        ["sessions", "create", "--workspace", str(tmp_path), "--title", "Review"],
    )

    assert created.exit_code == 0
    session_id = created.stdout.split()[-1]
    listed = runner.invoke(app, ["sessions", "list", "--workspace", str(tmp_path)])
    deleted = runner.invoke(
        app,
        ["sessions", "delete", session_id, "--workspace", str(tmp_path)],
        input="y\n",
    )

    assert "Review" in listed.stdout
    assert deleted.exit_code == 0
    assert "Session deleted" in deleted.stdout
