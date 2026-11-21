from typer.testing import CliRunner

from forge.cli import app


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


def test_prompt_reports_unimplemented_runner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["-p", "Explain this repository"])
    assert result.exit_code == 2
    assert "prompt runner is not implemented" in result.stdout


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


def test_search_command_reports_workspace_matches(tmp_path):
    (tmp_path / "main.py").write_text("forge is ready\n", encoding="utf-8")
    (tmp_path / ".env").write_text("forge secret\n", encoding="utf-8")

    result = runner.invoke(app, ["search", "forge", "--workspace", str(tmp_path)])

    assert result.exit_code == 0
    assert "main.py:1: forge is ready" in result.stdout
    assert "forge secret" not in result.stdout
