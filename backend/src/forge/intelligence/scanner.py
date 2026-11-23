from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from git import InvalidGitRepositoryError, Repo

from forge.context.workspace import Workspace, WorkspacePathError
from forge.intelligence.models import (
    GitMetadata,
    ProjectManifest,
    RepoInventory,
    RepoMap,
    ScannedFile,
)

logger = logging.getLogger("forge.intelligence.scanner")

DEFAULT_MAX_FILES = 5000
DEFAULT_MAX_FILE_BYTES = 1024 * 1024  # 1 MiB

EXTENSION_LANGUAGE_MAP: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "jsx",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".json": "json",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".md": "markdown",
    ".rst": "restructuredtext",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    ".sass": "sass",
    ".sql": "sql",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".ps1": "powershell",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c_header",
    ".hpp": "cpp_header",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".dockerfile": "dockerfile",
}

FILENAME_LANGUAGE_MAP: dict[str, str] = {
    "dockerfile": "dockerfile",
    "makefile": "makefile",
    "cmakelists.txt": "cmake",
    ".gitignore": "gitignore",
    ".env.example": "dotenv",
    ".env.sample": "dotenv",
    ".env.template": "dotenv",
}

TEST_FILE_PATTERNS = [
    re.compile(r"(?:^|/)(?:test_[^/]+|[^/]+_test)\.py$", re.IGNORECASE),
    re.compile(r"(?:^|/)tests?/.*\.py$", re.IGNORECASE),
    re.compile(r"(?:^|/)(?:[^/]+\.)(?:test|spec)\.[jt]sx?$", re.IGNORECASE),
    re.compile(r"(?:^|/)(?:tests?|__tests?__)/.*\.[jt]sx?$", re.IGNORECASE),
]

MANIFEST_FILENAMES = {
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
    "setup.cfg",
    "pipfile",
    "package.json",
    "pnpm-workspace.yaml",
    "tsconfig.json",
    "alembic.ini",
}


def detect_language(path: str | Path) -> str:
    p = Path(path)
    name_lower = p.name.lower()
    if name_lower in FILENAME_LANGUAGE_MAP:
        return FILENAME_LANGUAGE_MAP[name_lower]
    suffix = p.suffix.lower()
    return EXTENSION_LANGUAGE_MAP.get(suffix, "text")


def is_test_file(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/")
    return any(pattern.search(normalized) for pattern in TEST_FILE_PATTERNS)


class RepoScanner:
    """Bounded, security-conscious repository scanner producing structured inventory and repo maps."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        workspace: Workspace | None = None,
        max_files: int = DEFAULT_MAX_FILES,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        if not self.workspace_root.is_dir():
            raise ValueError(f"Workspace root is not a directory: {self.workspace_root}")
        self._workspace = workspace or Workspace(self.workspace_root)
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes

    def scan(self) -> RepoInventory:
        """Scan repository files, honoring exclusions, security bounds, and computing hashes."""
        start_time = time.perf_counter()
        scanned_files: list[ScannedFile] = []
        directories: set[str] = set()
        languages: dict[str, int] = defaultdict(int)
        manifests: list[ProjectManifest] = []
        truncated = False

        # Walk workspace directory safely without following symlinks
        for root, dirs, files in os.walk(self.workspace_root, followlinks=False):
            current_dir = Path(root)
            try:
                rel_dir = current_dir.relative_to(self.workspace_root)
            except ValueError:
                dirs.clear()
                continue

            # Check if directory is excluded or contains symlinks
            if rel_dir.parts:
                rel_dir_posix = rel_dir.as_posix()
                if self._workspace._is_excluded(rel_dir):
                    dirs.clear()
                    continue
                # Reject symlink directories
                if current_dir.is_symlink() or os.path.islink(current_dir):
                    dirs.clear()
                    continue
                directories.add(rel_dir_posix)

            # Prune excluded subdirectories in-place for efficiency
            dirs[:] = [
                d
                for d in dirs
                if not self._workspace._is_excluded(rel_dir / d)
                and not (current_dir / d).is_symlink()
                and not os.path.islink(current_dir / d)
            ]
            dirs.sort()

            # Process files in sorted order
            for file_name in sorted(files):
                if len(scanned_files) >= self.max_files:
                    truncated = True
                    break

                rel_file = rel_dir / file_name if rel_dir.parts else Path(file_name)
                rel_file_posix = rel_file.as_posix()

                # Security exclusion check
                if self._workspace._is_excluded(rel_file):
                    continue

                full_path = current_dir / file_name
                if full_path.is_symlink() or os.path.islink(full_path):
                    continue

                try:
                    stat_info = full_path.stat()
                except OSError:
                    continue

                # Skip non-regular files
                if not full_path.is_file():
                    continue

                size_bytes = stat_info.st_size
                mtime = stat_info.st_mtime
                lang = detect_language(rel_file)
                is_manifest = rel_file.name.lower() in MANIFEST_FILENAMES
                is_test = is_test_file(rel_file_posix)

                # Compute content hash
                content_sha256 = self._compute_file_sha256(full_path, size_bytes)

                scanned = ScannedFile(
                    relative_path=rel_file_posix,
                    language=lang,
                    size_bytes=size_bytes,
                    content_sha256=content_sha256,
                    mtime=mtime,
                    is_manifest=is_manifest,
                    is_test=is_test,
                )
                scanned_files.append(scanned)
                languages[lang] += 1

                # Parse manifest if recognized
                if is_manifest:
                    manifest = self._parse_manifest(rel_file_posix, full_path)
                    if manifest is not None:
                        manifests.append(manifest)

            if truncated:
                break

        git_meta = self._get_git_metadata()
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        # Sort files deterministically by path
        scanned_files.sort(key=lambda f: f.relative_path)
        sorted_dirs = sorted(directories)

        return RepoInventory(
            workspace_root=str(self.workspace_root),
            files=scanned_files,
            directories=sorted_dirs,
            languages=dict(sorted(languages.items(), key=lambda item: (-item[1], item[0]))),
            manifests=manifests,
            git_meta=git_meta,
            scan_time_ms=elapsed_ms,
            truncated=truncated,
        )

    def _compute_file_sha256(self, path: Path, size_bytes: int = 0) -> str:
        """Compute the streaming SHA-256 hash of the complete file."""
        hasher = hashlib.sha256()
        try:
            with path.open("rb") as f:
                while chunk := f.read(65536):
                    hasher.update(chunk)
        except OSError:
            return ""
        return hasher.hexdigest()

    def _get_git_metadata(self) -> GitMetadata | None:
        try:
            repo = Repo(self.workspace_root, search_parent_directories=True)
            branch = None
            try:
                branch = repo.active_branch.name
            except (TypeError, ValueError):
                try:
                    branch = repo.head.commit.hexsha[:7]
                except Exception:
                    pass

            commit_sha = None
            try:
                commit_sha = repo.head.commit.hexsha
            except Exception:
                pass

            is_dirty = repo.is_dirty(untracked_files=False)
            modified_files: list[str] = []
            try:
                diff_index = repo.index.diff(None)
                for item in diff_index:
                    if item.a_path:
                        modified_files.append(item.a_path.replace("\\", "/"))
            except Exception:
                pass

            return GitMetadata(
                branch=branch,
                commit_sha=commit_sha,
                is_dirty=is_dirty,
                modified_files=sorted(set(modified_files)),
            )
        except (InvalidGitRepositoryError, Exception):
            return None

    def _parse_manifest(self, rel_path: str, full_path: Path) -> ProjectManifest | None:
        filename = full_path.name.lower()
        try:
            if filename == "pyproject.toml":
                return self._parse_pyproject_toml(rel_path, full_path)
            elif filename == "package.json":
                return self._parse_package_json(rel_path, full_path)
            elif filename == "requirements.txt":
                return self._parse_requirements_txt(rel_path, full_path)
        except Exception as exc:
            logger.debug("Failed to parse manifest %s: %s", rel_path, exc)
        return None

    def _parse_pyproject_toml(self, rel_path: str, full_path: Path) -> ProjectManifest:
        text = full_path.read_text(encoding="utf-8", errors="replace")
        deps: list[str] = []
        dev_deps: list[str] = []
        scripts: dict[str, str] = {}
        frameworks: list[str] = []
        name = None

        # Parse basic TOML sections with lightweight regex to avoid external parser dependency
        in_project_deps = False
        in_dev_deps = False
        in_scripts = False

        for line in text.splitlines():
            line_str = line.strip()
            if not line_str or line_str.startswith("#"):
                continue

            if line_str.startswith("[") and line_str.endswith("]"):
                header = line_str.strip("[]").strip()
                in_project_deps = header == "project.dependencies" or header == "tool.poetry.dependencies"
                in_dev_deps = "optional-dependencies" in header or "dev-dependencies" in header or "group.dev.dependencies" in header
                in_scripts = header == "project.scripts" or header == "tool.poetry.scripts"
                continue

            if line_str.startswith("name ="):
                m = re.search(r'name\s*=\s*["\']([^"\']+)["\']', line_str)
                if m:
                    name = m.group(1)

            if "dependencies = [" in line_str:
                in_project_deps = True
                continue
            if "dev = [" in line_str or "dev-dependencies = [" in line_str:
                in_dev_deps = True
                continue

            if in_project_deps:
                if line_str == "]":
                    in_project_deps = False
                    continue
                dep_match = re.search(r'["\']([a-zA-Z0-9_\-\.]+)(?:[><=~^!].*)?["\']', line_str)
                if dep_match:
                    dep = dep_match.group(1).lower()
                    deps.append(dep)
                    if dep in {"fastapi", "flask", "django", "pytest", "sqlalchemy", "pydantic", "typer", "uvicorn"}:
                        frameworks.append(dep)

            if in_dev_deps:
                if line_str == "]":
                    in_dev_deps = False
                    continue
                dep_match = re.search(r'["\']([a-zA-Z0-9_\-\.]+)(?:[><=~^!].*)?["\']', line_str)
                if dep_match:
                    dev_dep = dep_match.group(1).lower()
                    dev_deps.append(dev_dep)
                    if dev_dep in {"pytest", "ruff", "mypy", "black", "flake8"}:
                        frameworks.append(dev_dep)

            if in_scripts and "=" in line_str:
                parts = line_str.split("=", 1)
                script_name = parts[0].strip().strip('"\'')
                script_cmd = parts[1].strip().strip('"\'')
                scripts[script_name] = script_cmd

        return ProjectManifest(
            ecosystem="python",
            manifest_path=rel_path,
            name=name,
            dependencies=sorted(set(deps)),
            dev_dependencies=sorted(set(dev_deps)),
            scripts=scripts,
            frameworks=sorted(set(frameworks)),
        )

    def _parse_package_json(self, rel_path: str, full_path: Path) -> ProjectManifest:
        data = json.loads(full_path.read_text(encoding="utf-8", errors="replace"))
        name = data.get("name")
        raw_deps = data.get("dependencies", {})
        raw_dev_deps = data.get("devDependencies", {})
        scripts = {k: str(v) for k, v in data.get("scripts", {}).items() if isinstance(v, str)}

        deps = list(raw_deps.keys()) if isinstance(raw_deps, dict) else []
        dev_deps = list(raw_dev_deps.keys()) if isinstance(raw_dev_deps, dict) else []

        frameworks: list[str] = []
        all_deps = set(deps + dev_deps)
        known_frameworks = {
            "react", "vue", "svelte", "next", "vite", "vitest", "jest",
            "express", "fastify", "nest", "tailwindcss", "typescript"
        }
        for d in all_deps:
            d_clean = d.lower().lstrip("@")
            if d_clean in known_frameworks or any(d_clean.startswith(f"{kf}/") for kf in known_frameworks):
                frameworks.append(d)

        return ProjectManifest(
            ecosystem="node",
            manifest_path=rel_path,
            name=name,
            dependencies=sorted(deps),
            dev_dependencies=sorted(dev_deps),
            scripts=scripts,
            frameworks=sorted(set(frameworks)),
        )

    def _parse_requirements_txt(self, rel_path: str, full_path: Path) -> ProjectManifest:
        text = full_path.read_text(encoding="utf-8", errors="replace")
        deps: list[str] = []
        frameworks: list[str] = []
        for line in text.splitlines():
            line_str = line.strip()
            if not line_str or line_str.startswith("#") or line_str.startswith("-"):
                continue
            m = re.match(r"^([a-zA-Z0-9_\-\.]+)", line_str)
            if m:
                dep = m.group(1).lower()
                deps.append(dep)
                if dep in {"fastapi", "flask", "django", "pytest", "sqlalchemy", "pydantic"}:
                    frameworks.append(dep)
        return ProjectManifest(
            ecosystem="python",
            manifest_path=rel_path,
            name=None,
            dependencies=sorted(set(deps)),
            dev_dependencies=[],
            scripts={},
            frameworks=sorted(set(frameworks)),
        )

    def generate_repo_map(self, inventory: RepoInventory, *, max_tree_lines: int = 100) -> RepoMap:
        """Generate a deterministic repository map with directory hierarchy and project boundaries."""
        total_files = len(inventory.files)
        total_bytes = sum(f.size_bytes for f in inventory.files)

        # Detect project boundaries
        boundaries: list[str] = []
        for m in inventory.manifests:
            p = Path(m.manifest_path)
            project_dir = p.parent.as_posix() if p.parent.parts else "root"
            details = [f"ecosystem: {m.ecosystem}"]
            if m.name:
                details.append(f"name: {m.name}")
            if m.frameworks:
                details.append(f"frameworks: {', '.join(m.frameworks)}")
            boundaries.append(f"{project_dir} ({'; '.join(details)})")

        # Build directory summary
        dir_counts: dict[str, int] = defaultdict(int)
        for f in inventory.files:
            p = Path(f.relative_path)
            parent = p.parent.as_posix() if p.parent.parts else "."
            dir_counts[parent] += 1

        # Build deterministic tree
        tree_lines: list[str] = []
        tree_lines.append(f"📁 {Path(inventory.workspace_root).name}/")

        sorted_files = [f.relative_path for f in inventory.files]
        # Show top-level overview and primary directories
        tree_nodes: dict[str, list[str]] = defaultdict(list)
        for path_str in sorted_files:
            parts = path_str.split("/")
            if len(parts) == 1:
                tree_nodes["."].append(parts[0])
            else:
                top_dir = parts[0]
                tree_nodes[top_dir].append("/".join(parts[1:]))

        for top_dir in sorted(tree_nodes.keys()):
            if top_dir == ".":
                for f_name in tree_nodes["."]:
                    if len(tree_lines) < max_tree_lines:
                        tree_lines.append(f"  ├── {f_name}")
            else:
                count = len(tree_nodes[top_dir])
                if len(tree_lines) < max_tree_lines:
                    tree_lines.append(f"  ├── 📁 {top_dir}/ ({count} files)")
                    # Show first few files in top directory
                    sub_files = tree_nodes[top_dir][:5]
                    for sf in sub_files:
                        if len(tree_lines) < max_tree_lines:
                            tree_lines.append(f"  │   ├── {sf}")
                    if count > 5 and len(tree_lines) < max_tree_lines:
                        tree_lines.append(f"  │   └── ... ({count - 5} more files)")

        if len(tree_lines) >= max_tree_lines:
            tree_lines.append(f"  └── ... (truncated at {max_tree_lines} lines)")

        tree_text = "\n".join(tree_lines)

        # Summary text
        lang_summary = ", ".join(f"{lang}: {cnt}" for lang, cnt in list(inventory.languages.items())[:6])
        summary_lines = [
            f"Repository Map: {total_files} files, {total_bytes:,} bytes across {len(inventory.directories)} directories.",
            f"Primary languages: {lang_summary or 'none'}.",
        ]
        if boundaries:
            summary_lines.append("Projects & boundaries: " + " | ".join(boundaries))
        if inventory.git_meta and inventory.git_meta.branch:
            dirty_str = " (dirty)" if inventory.git_meta.is_dirty else ""
            summary_lines.append(f"Git: branch '{inventory.git_meta.branch}'{dirty_str}, commit {inventory.git_meta.commit_sha or 'unknown'}")

        summary_text = "\n".join(summary_lines)

        return RepoMap(
            summary_text=summary_text,
            tree_text=tree_text,
            languages=inventory.languages,
            total_files=total_files,
            total_bytes=total_bytes,
            project_boundaries=boundaries,
        )
