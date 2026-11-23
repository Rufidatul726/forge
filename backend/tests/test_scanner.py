import json
from pathlib import Path

import pytest

from forge.context.workspace import Workspace
from forge.intelligence.scanner import RepoScanner, detect_language, is_test_file


def test_detect_language():
    assert detect_language("foo.py") == "python"
    assert detect_language("src/app.ts") == "typescript"
    assert detect_language("src/App.tsx") == "tsx"
    assert detect_language("script.js") == "javascript"
    assert detect_language("component.jsx") == "jsx"
    assert detect_language("Dockerfile") == "dockerfile"
    assert detect_language("pyproject.toml") == "toml"
    assert detect_language("unknown.xyz") == "text"


def test_is_test_file():
    assert is_test_file("tests/test_scanner.py") is True
    assert is_test_file("src/test_foo.py") is True
    assert is_test_file("src/foo_test.py") is True
    assert is_test_file("frontend/src/App.test.tsx") is True
    assert is_test_file("frontend/src/App.spec.ts") is True
    assert is_test_file("src/main.py") is False
    assert is_test_file("frontend/src/index.ts") is False


def test_repo_scanner_basic(tmp_path: Path):
    (tmp_path / "main.py").write_text("print('hello world')\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "util.py").write_text("def helper(): pass\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Test Repo\n", encoding="utf-8")

    scanner = RepoScanner(tmp_path)
    inv = scanner.scan()

    assert len(inv.files) == 3
    rel_paths = [f.relative_path for f in inv.files]
    assert "README.md" in rel_paths
    assert "main.py" in rel_paths
    assert "src/util.py" in rel_paths

    main_f = next(f for f in inv.files if f.relative_path == "main.py")
    assert main_f.language == "python"
    assert main_f.size_bytes > 0
    assert len(main_f.content_sha256) == 64
    assert main_f.is_test is False

    repo_map = scanner.generate_repo_map(inv)
    assert "Repository Map:" in repo_map.summary_text
    assert "main.py" in repo_map.tree_text


def test_repo_scanner_exclusions_and_security(tmp_path: Path):
    # Create allowed files
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    # Sensitive files that must be excluded
    (tmp_path / ".env").write_text("SECRET=12345\n", encoding="utf-8")
    (tmp_path / "credentials.json").write_text('{"token": "xyz"}', encoding="utf-8")

    # Ignored directories
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("console.log(1);", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("dummy", encoding="utf-8")
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "internal.txt").write_text("internal", encoding="utf-8")

    # .gitignore rules
    (tmp_path / ".gitignore").write_text("*.log\ntemp/\n", encoding="utf-8")
    (tmp_path / "error.log").write_text("error", encoding="utf-8")
    (tmp_path / "temp").mkdir()
    (tmp_path / "temp" / "data.csv").write_text("1,2,3", encoding="utf-8")

    scanner = RepoScanner(tmp_path)
    inv = scanner.scan()

    scanned_paths = [f.relative_path for f in inv.files]
    assert "app.py" in scanned_paths
    assert ".gitignore" in scanned_paths

    # Must NOT include sensitive or excluded files
    assert ".env" not in scanned_paths
    assert "credentials.json" not in scanned_paths
    assert "error.log" not in scanned_paths
    assert not any("node_modules" in p for p in scanned_paths)
    assert not any(p.startswith(".git/") or p == ".git" for p in scanned_paths)
    assert not any(p.startswith(".forge/") or p == ".forge" for p in scanned_paths)
    assert not any(p.startswith("temp/") or p == "temp" for p in scanned_paths)


def test_repo_scanner_manifest_detection(tmp_path: Path):
    # Python pyproject.toml
    pyproject_content = """[project]
name = "my-service"
version = "0.1.0"
dependencies = [
    "fastapi>=0.100.0",
    "pydantic",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
]

[project.scripts]
my-cli = "my_service.cli:main"
"""
    (tmp_path / "pyproject.toml").write_text(pyproject_content, encoding="utf-8")

    # Node package.json
    (tmp_path / "frontend").mkdir()
    pkg_json = {
        "name": "my-ui",
        "dependencies": {
            "react": "^19.0.0",
        },
        "devDependencies": {
            "vite": "^6.0.0",
            "vitest": "^3.0.0",
        },
        "scripts": {
            "build": "vite build",
        },
    }
    (tmp_path / "frontend" / "package.json").write_text(json.dumps(pkg_json), encoding="utf-8")

    scanner = RepoScanner(tmp_path)
    inv = scanner.scan()

    assert len(inv.manifests) == 2
    py_manifest = next(m for m in inv.manifests if m.ecosystem == "python")
    assert py_manifest.name == "my-service"
    assert "fastapi" in py_manifest.dependencies
    assert "pytest" in py_manifest.dev_dependencies
    assert "fastapi" in py_manifest.frameworks
    assert "pytest" in py_manifest.frameworks
    assert "my-cli" in py_manifest.scripts

    node_manifest = next(m for m in inv.manifests if m.ecosystem == "node")
    assert node_manifest.name == "my-ui"
    assert "react" in node_manifest.dependencies
    assert "vite" in node_manifest.dev_dependencies
    assert "react" in node_manifest.frameworks

    repo_map = scanner.generate_repo_map(inv)
    assert any("my-service" in b for b in repo_map.project_boundaries)
    assert any("my-ui" in b for b in repo_map.project_boundaries)
