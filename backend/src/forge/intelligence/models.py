from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class SymbolKind(str, Enum):
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    INTERFACE = "interface"
    TYPE_ALIAS = "type"
    CONSTANT = "constant"
    VARIABLE = "variable"
    MODULE = "module"


class ReferenceKind(str, Enum):
    CALL = "call"
    INSTANTIATION = "instantiation"
    INHERITANCE = "inheritance"
    USAGE = "usage"
    IMPORT = "import"


@dataclass(frozen=True)
class ScannedFile:
    relative_path: str
    language: str
    size_bytes: int
    content_sha256: str
    mtime: float
    is_manifest: bool = False
    is_test: bool = False


@dataclass(frozen=True)
class GitMetadata:
    branch: str | None = None
    commit_sha: str | None = None
    is_dirty: bool = False
    modified_files: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProjectManifest:
    ecosystem: str  # e.g., "python", "node"
    manifest_path: str  # e.g., "backend/pyproject.toml"
    name: str | None = None
    dependencies: list[str] = field(default_factory=list)
    dev_dependencies: list[str] = field(default_factory=list)
    scripts: dict[str, str] = field(default_factory=dict)
    frameworks: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RepoInventory:
    workspace_root: str
    files: list[ScannedFile]
    directories: list[str]
    languages: dict[str, int]
    manifests: list[ProjectManifest]
    git_meta: GitMetadata | None = None
    scan_time_ms: float = 0.0
    truncated: bool = False


@dataclass(frozen=True)
class SymbolRecord:
    name: str
    kind: SymbolKind
    path: str
    line_number: int
    end_line_number: int
    parent_name: str | None = None
    signature: str | None = None
    docstring: str | None = None


@dataclass(frozen=True)
class ImportRecord:
    source_path: str
    module: str
    imported_names: list[str] = field(default_factory=list)
    alias: str | None = None
    line_number: int = 1
    is_relative: bool = False


@dataclass(frozen=True)
class ReferenceRecord:
    source_path: str
    symbol_name: str
    line_number: int
    kind: ReferenceKind = ReferenceKind.USAGE
    target_path: str | None = None
    target_symbol_name: str | None = None
    is_resolved: bool = False


@dataclass(frozen=True)
class ExtractedFileSymbols:
    path: str
    symbols: list[SymbolRecord] = field(default_factory=list)
    imports: list[ImportRecord] = field(default_factory=list)
    references: list[ReferenceRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DependencyRecord:
    ecosystem: str
    name: str
    version_constraint: str | None = None
    scope: str = "runtime"  # runtime or dev
    source_file: str = ""


@dataclass(frozen=True)
class RepoMap:
    summary_text: str
    tree_text: str
    languages: dict[str, int]
    total_files: int
    total_bytes: int
    project_boundaries: list[str] = field(default_factory=list)
