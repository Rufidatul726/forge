from pathlib import Path
from typer.testing import CliRunner

from forge.cli import app

runner = CliRunner()


def test_cli_index_and_status(tmp_path: Path):
    (tmp_path / "main.py").write_text("def hello(): pass\n", encoding="utf-8")

    # 1. forge index
    res_index = runner.invoke(app, ["index", "-w", str(tmp_path)])
    assert res_index.exit_code == 0
    assert "Indexed repository successfully" in res_index.output

    # 2. forge index --status
    res_status = runner.invoke(app, ["index", "--status", "-w", str(tmp_path)])
    assert res_status.exit_code == 0
    assert "Repository Intelligence Index Status" in res_status.output
    assert "Total Files Indexed" in res_status.output

    # 3. forge index --force
    res_force = runner.invoke(app, ["index", "--force", "-w", str(tmp_path)])
    assert res_force.exit_code == 0
    assert "Indexed repository successfully" in res_force.output


def test_cli_map(tmp_path: Path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "util.ts").write_text("export const PI = 3.14;\n", encoding="utf-8")

    res_map = runner.invoke(app, ["map", "-w", str(tmp_path)])
    assert res_map.exit_code == 0
    assert "Repository Intelligence Map" in res_map.output
    assert "app.py" in res_map.output


def test_cli_symbols_and_references(tmp_path: Path):
    code = """from math import sqrt
def calculate():
    return sqrt(16)
"""
    (tmp_path / "math_util.py").write_text(code, encoding="utf-8")

    # Index first
    runner.invoke(app, ["index", "-w", str(tmp_path)])

    # forge symbols
    res_sym = runner.invoke(app, ["symbols", "calculate", "-w", str(tmp_path)])
    assert res_sym.exit_code == 0
    assert "calculate" in res_sym.output

    # forge references
    res_refs = runner.invoke(app, ["references", "sqrt", "-w", str(tmp_path)])
    assert res_refs.exit_code == 0
    assert "sqrt" in res_refs.output
    assert "math_util.py" in res_refs.output


def test_cli_explain(tmp_path: Path):
    code = """import os
class MyService:
    def execute(self):
        pass
"""
    (tmp_path / "service.py").write_text(code, encoding="utf-8")
    runner.invoke(app, ["index", "-w", str(tmp_path)])

    res_exp = runner.invoke(app, ["explain", "service.py", "-w", str(tmp_path)])
    assert res_exp.exit_code == 0
    assert "Explain File: service.py" in res_exp.output
    assert "MyService" in res_exp.output
    assert "execute" in res_exp.output


def test_cli_context(tmp_path: Path):
    (tmp_path / "FORGE.md").write_text("# Project instructions\n", encoding="utf-8")
    (tmp_path / "user.py").write_text("def register_user(): pass\n", encoding="utf-8")
    runner.invoke(app, ["index", "-w", str(tmp_path)])

    res_ctx = runner.invoke(app, ["context", "register_user task", "--budget", "2000", "-w", str(tmp_path)])
    assert res_ctx.exit_code == 0
    assert "Context Retrieval Manifest" in res_ctx.output
    assert "Context Budget Summary" in res_ctx.output
    assert "user.py" in res_ctx.output
