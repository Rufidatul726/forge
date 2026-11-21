import pytest

from forge.context import (
    Workspace,
    WorkspaceEncodingError,
    WorkspaceFileTooLargeError,
    WorkspacePathError,
)


def test_read_text_is_relative_and_workspace_bounded(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "README.md").write_bytes(b"Forge workspace\n")
    workspace = Workspace(root)

    assert workspace.read_text("README.md") == "Forge workspace\n"
    with pytest.raises(WorkspacePathError, match="relative path"):
        workspace.read_text("../outside.txt")
    with pytest.raises(WorkspacePathError, match="relative path"):
        workspace.read_text("C:\\outside.txt")


def test_read_text_rejects_sensitive_files_but_allows_examples(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (tmp_path / ".env.local").write_text("TOKEN=secret", encoding="utf-8")
    (tmp_path / ".env.example").write_bytes(b"TOKEN=\n")
    (tmp_path / "private.pem").write_text("private key", encoding="utf-8")
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_ed25519").write_text("private key", encoding="utf-8")
    workspace = Workspace(tmp_path)

    assert workspace.read_text(".env.example") == "TOKEN=\n"
    for path in (".env", ".env.local", "private.pem", ".ssh/id_ed25519"):
        with pytest.raises(WorkspacePathError):
            workspace.read_text(path)


def test_read_text_rejects_files_over_limit_and_non_utf8(tmp_path):
    (tmp_path / "large.txt").write_text("x" * 17, encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"\xff\xfe")
    (tmp_path / "nul.bin").write_bytes(b"forge\x00secret")
    workspace = Workspace(tmp_path, max_file_bytes=16)

    with pytest.raises(WorkspaceFileTooLargeError):
        workspace.read_text("large.txt")
    with pytest.raises(WorkspaceEncodingError):
        workspace.read_text("binary.bin")
    with pytest.raises(WorkspaceEncodingError):
        workspace.read_text("nul.bin")


def test_read_text_rejects_symlink_outside_workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("outside workspace", encoding="utf-8")
    link = root / "linked.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("File symlinks are unavailable on this system")
    if not link.is_symlink():
        pytest.skip("File symlinks are unavailable on this system")

    with pytest.raises(WorkspacePathError):
        Workspace(root).read_text("linked.txt")


def test_read_text_rejects_alias_to_sensitive_file(tmp_path, monkeypatch):
    secret = tmp_path / ".env.local"
    secret.write_text("TOKEN=secret", encoding="utf-8")
    alias = tmp_path / "notes.txt"
    alias.write_text("not a secret", encoding="utf-8")
    original_resolve = type(tmp_path).resolve

    def resolve_with_sensitive_alias(path, strict=False):
        if path == alias:
            return original_resolve(secret, strict=True)
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(type(tmp_path), "resolve", resolve_with_sensitive_alias)

    workspace = Workspace(tmp_path)
    with pytest.raises(WorkspacePathError):
        workspace.read_text(alias.name)
    assert "notes.txt" not in workspace.list_files().paths


def test_list_files_excludes_private_and_generated_paths(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('forge')", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dependency.js").write_text("", encoding="utf-8")
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / ".pytest_cache" / "state").write_text("generated", encoding="utf-8")
    (tmp_path / ".env.local").write_text("secret", encoding="utf-8")
    (tmp_path / ".env.example").write_text("placeholder", encoding="utf-8")
    (tmp_path / "signing.key").write_text("secret", encoding="utf-8")

    result = Workspace(tmp_path).list_files()
    assert result.paths == [".env.example", "src/app.py"]
    assert result.truncated is False


def test_list_files_applies_pattern_and_reports_truncation(tmp_path):
    for name in ("a.py", "b.py", "c.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    workspace = Workspace(tmp_path)

    result = workspace.list_files("*.py", limit=1)
    assert len(result.paths) == 1
    assert result.truncated is True
    with pytest.raises(WorkspacePathError):
        workspace.list_files("../**/*")


def test_list_files_stops_after_scan_limit(tmp_path):
    for index in range(5):
        (tmp_path / f"{index}.txt").write_text("file", encoding="utf-8")
    result = Workspace(tmp_path, max_files_scanned=2).list_files()
    assert len(result.paths) <= 2
    assert result.truncated is True


def test_search_text_reports_lines_case_and_truncation(tmp_path):
    (tmp_path / "first.txt").write_text("Forge begins\nother\nforge continues\n", encoding="utf-8")
    (tmp_path / "second.txt").write_text("FORGE ends\n", encoding="utf-8")
    workspace = Workspace(tmp_path)

    matches = workspace.search_text("forge")
    assert [(match.path, match.line_number) for match in matches.matches] == [
        ("first.txt", 1),
        ("first.txt", 3),
        ("second.txt", 1),
    ]
    assert matches.truncated is False

    limited = workspace.search_text("forge", limit=2)
    assert len(limited.matches) == 2
    assert limited.truncated is True

    case_sensitive = workspace.search_text("FORGE", case_sensitive=True)
    assert [(match.path, match.line_number) for match in case_sensitive.matches] == [
        ("second.txt", 1)
    ]


def test_search_skips_binary_and_oversized_files(tmp_path):
    (tmp_path / "binary.dat").write_bytes(b"forge\xff")
    (tmp_path / "nul.dat").write_bytes(b"forge\x00secret")
    (tmp_path / "large.txt").write_text("forge" * 10, encoding="utf-8")
    (tmp_path / "readable.txt").write_text("forge is readable", encoding="utf-8")

    result = Workspace(tmp_path, max_file_bytes=20).search_text("forge")
    assert [match.path for match in result.matches] == ["readable.txt"]
    assert result.files_skipped == 3


def test_search_stops_at_candidate_limit(tmp_path):
    for index in range(3):
        (tmp_path / f"{index}.txt").write_text("needle", encoding="utf-8")

    result = Workspace(tmp_path, max_files_scanned=1).search_text("needle")
    assert result.files_scanned == 1
    assert result.truncated is True


def test_search_reports_truncated_match_lines(tmp_path):
    (tmp_path / "long.txt").write_text("needle" + "x" * 2500, encoding="utf-8")

    match = Workspace(tmp_path).search_text("needle").matches[0]
    assert len(match.line) == 2000
    assert match.line_truncated is True


def test_search_rejects_empty_query_and_unbounded_pattern(tmp_path):
    workspace = Workspace(tmp_path)
    with pytest.raises(ValueError, match="must not be empty"):
        workspace.search_text("")
    with pytest.raises(WorkspacePathError):
        workspace.search_text("needle", pattern="../../**/*")


def test_discover_context_loads_instructions_and_file_inventory(tmp_path):
    (tmp_path / "FORGE.md").write_text("Project rules", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hello')", encoding="utf-8")
    (tmp_path / ".env").write_text("secret", encoding="utf-8")

    context = Workspace(tmp_path).discover_context()
    assert context.root == tmp_path.resolve()
    assert context.instructions == "Project rules"
    assert "FORGE.md" in context.files.paths
    assert "src/main.py" in context.files.paths
    assert ".env" not in context.files.paths


def test_workspace_requires_an_existing_directory(tmp_path):
    with pytest.raises(WorkspacePathError):
        Workspace(tmp_path / "missing")

    file_path = tmp_path / "file"
    file_path.write_text("file", encoding="utf-8")
    with pytest.raises(WorkspacePathError):
        Workspace(file_path)
