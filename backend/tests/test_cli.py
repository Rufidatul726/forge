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
