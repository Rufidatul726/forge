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
