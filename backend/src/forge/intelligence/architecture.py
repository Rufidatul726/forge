from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge.intelligence.models import (
    ExtractedFileSymbols,
    ProjectManifest,
    RepoInventory,
    SymbolKind,
)

RE_API_ROUTE = re.compile(
    r"""@(?:(?:app|router|api_router)\.(?P<method>get|post|put|delete|patch|options|head))\s*\(\s*['"](?P<route>[^'"]+)['"]""",
    re.IGNORECASE,
)
RE_EXPRESS_ROUTE = re.compile(
    r"""(?:app|router)\.(?P<method>get|post|put|delete|patch)\s*\(\s*['"](?P<route>[^'"]+)['"]""",
    re.IGNORECASE,
)
RE_SQLALCHEMY_MODEL = re.compile(r"""class\s+(?P<name>[a-zA-Z0-9_]+)\s*\([^)]*(?:Base|Model|declarative_base)[^)]*\):""")
RE_DJANGO_MODEL = re.compile(r"""class\s+(?P<name>[a-zA-Z0-9_]+)\s*\([^)]*models\.Model[^)]*\):""")


@dataclass(frozen=True)
class ApiRouteRecord:
    method: str
    path: str
    source_file: str
    line_number: int
    handler_symbol: str | None = None


@dataclass(frozen=True)
class DatabaseModelRecord:
    name: str
    framework: str  # "sqlalchemy", "django", "prisma", etc.
    source_file: str
    line_number: int


@dataclass(frozen=True)
class TestSuiteConfig:
    framework: str
    config_file: str | None
    test_directories: list[str]
    test_files_count: int


@dataclass(frozen=True)
class ArchitectureBoundary:
    name: str  # "backend", "frontend", "shared", etc.
    directory: str
    ecosystem: str
    frameworks: list[str]
    confidence: str = "high"


@dataclass(frozen=True)
class ModuleCoupling:
    from_module: str
    to_module: str
    import_count: int
    relationship: str = "static_import"
    confidence: str = "high"


@dataclass(frozen=True)
class ArchitectureSummary:
    boundaries: list[ArchitectureBoundary]
    api_routes: list[ApiRouteRecord]
    database_models: list[DatabaseModelRecord]
    migration_directories: list[str]
    test_suites: list[TestSuiteConfig]
    module_couplings: list[ModuleCoupling]
    manifests: list[ProjectManifest]


class ArchitectureAnalyzer:
    """Discovers architectural boundaries, routes, models, tests, and module relationships."""

    def analyze(
        self,
        inventory: RepoInventory,
        parsed_files: dict[str, ExtractedFileSymbols],
        file_contents: dict[str, str] | None = None,
    ) -> ArchitectureSummary:
        file_contents = file_contents or {}

        boundaries = self._detect_boundaries(inventory)
        api_routes = self._detect_api_routes(parsed_files, file_contents)
        db_models = self._detect_db_models(parsed_files, file_contents)
        migration_dirs = self._detect_migration_dirs(inventory)
        test_suites = self._detect_test_suites(inventory)
        couplings = self._detect_module_couplings(parsed_files)

        return ArchitectureSummary(
            boundaries=boundaries,
            api_routes=api_routes,
            database_models=db_models,
            migration_directories=migration_dirs,
            test_suites=test_suites,
            module_couplings=couplings,
            manifests=inventory.manifests,
        )

    def _detect_boundaries(self, inventory: RepoInventory) -> list[ArchitectureBoundary]:
        boundaries: list[ArchitectureBoundary] = []
        for m in inventory.manifests:
            p = Path(m.manifest_path)
            dir_str = p.parent.as_posix() if p.parent.parts else "."
            name = dir_str
            if "backend" in dir_str.lower() or m.ecosystem == "python":
                name = "backend" if dir_str != "." else "root (backend)"
            elif "frontend" in dir_str.lower() or "client" in dir_str.lower() or "ui" in dir_str.lower():
                name = "frontend"

            boundaries.append(
                ArchitectureBoundary(
                    name=name,
                    directory=dir_str,
                    ecosystem=m.ecosystem,
                    frameworks=m.frameworks,
                    confidence="high",
                )
            )

        # If no manifests found, infer from directories
        if not boundaries:
            for d in inventory.directories:
                d_lower = d.lower()
                if d_lower in {"backend", "server", "api"}:
                    boundaries.append(
                        ArchitectureBoundary(
                            name="backend",
                            directory=d,
                            ecosystem="unknown",
                            frameworks=[],
                            confidence="inferred_directory",
                        )
                    )
                elif d_lower in {"frontend", "client", "web", "ui"}:
                    boundaries.append(
                        ArchitectureBoundary(
                            name="frontend",
                            directory=d,
                            ecosystem="unknown",
                            frameworks=[],
                            confidence="inferred_directory",
                        )
                    )
        return boundaries

    def _detect_api_routes(
        self,
        parsed_files: dict[str, ExtractedFileSymbols],
        file_contents: dict[str, str],
    ) -> list[ApiRouteRecord]:
        routes: list[ApiRouteRecord] = []
        for path, content in file_contents.items():
            lines = content.splitlines()
            for line_idx, line in enumerate(lines, start=1):
                # FastAPI / Starlette / Flask
                m_py = RE_API_ROUTE.search(line)
                if m_py:
                    # Look ahead for handler function
                    handler = None
                    if line_idx < len(lines):
                        next_line = lines[line_idx]
                        if "def " in next_line:
                            handler_match = re.search(r"def\s+([a-zA-Z0-9_]+)", next_line)
                            if handler_match:
                                handler = handler_match.group(1)

                    routes.append(
                        ApiRouteRecord(
                            method=m_py.group("method").upper(),
                            path=m_py.group("route"),
                            source_file=path,
                            line_number=line_idx,
                            handler_symbol=handler,
                        )
                    )
                    continue

                # Express / Node
                m_js = RE_EXPRESS_ROUTE.search(line)
                if m_js:
                    routes.append(
                        ApiRouteRecord(
                            method=m_js.group("method").upper(),
                            path=m_js.group("route"),
                            source_file=path,
                            line_number=line_idx,
                        )
                    )
        routes.sort(key=lambda r: (r.path, r.method, r.source_file))
        return routes

    def _detect_db_models(
        self,
        parsed_files: dict[str, ExtractedFileSymbols],
        file_contents: dict[str, str],
    ) -> list[DatabaseModelRecord]:
        models: list[DatabaseModelRecord] = []
        for path, content in file_contents.items():
            for line_idx, line in enumerate(content.splitlines(), start=1):
                m_sql = RE_SQLALCHEMY_MODEL.search(line)
                if m_sql:
                    models.append(
                        DatabaseModelRecord(
                            name=m_sql.group("name"),
                            framework="sqlalchemy",
                            source_file=path,
                            line_number=line_idx,
                        )
                    )
                m_dj = RE_DJANGO_MODEL.search(line)
                if m_dj:
                    models.append(
                        DatabaseModelRecord(
                            name=m_dj.group("name"),
                            framework="django",
                            source_file=path,
                            line_number=line_idx,
                        )
                    )
        return models

    def _detect_migration_dirs(self, inventory: RepoInventory) -> list[str]:
        migration_dirs: list[str] = []
        for d in inventory.directories:
            name = Path(d).name.lower()
            if name in {"migrations", "alembic"} or "prisma/migrations" in d:
                migration_dirs.append(d)
        return sorted(migration_dirs)

    def _detect_test_suites(self, inventory: RepoInventory) -> list[TestSuiteConfig]:
        suites: list[TestSuiteConfig] = []
        test_files = [f for f in inventory.files if f.is_test]

        # Partition test files by top-level project
        py_tests = [f for f in test_files if f.language == "python"]
        js_tests = [f for f in test_files if f.language in {"typescript", "javascript", "tsx", "jsx"}]

        if py_tests:
            dirs = sorted(set(Path(f.relative_path).parent.as_posix() for f in py_tests))
            config_file = next((f.relative_path for f in inventory.files if f.relative_path.endswith("pytest.ini") or f.relative_path.endswith("setup.cfg")), None)
            suites.append(
                TestSuiteConfig(
                    framework="pytest",
                    config_file=config_file,
                    test_directories=dirs,
                    test_files_count=len(py_tests),
                )
            )

        if js_tests:
            dirs = sorted(set(Path(f.relative_path).parent.as_posix() for f in js_tests))
            config_file = next((f.relative_path for f in inventory.files if "vitest.config" in f.relative_path or "jest.config" in f.relative_path), None)
            suites.append(
                TestSuiteConfig(
                    framework="vitest/jest",
                    config_file=config_file,
                    test_directories=dirs,
                    test_files_count=len(js_tests),
                )
            )

        return suites

    def _detect_module_couplings(
        self,
        parsed_files: dict[str, ExtractedFileSymbols],
    ) -> list[ModuleCoupling]:
        pair_counts: dict[tuple[str, str], int] = defaultdict(int)

        for path, extracted in parsed_files.items():
            from_parts = path.split("/")
            from_mod = from_parts[0] if len(from_parts) > 1 else "."

            for imp in extracted.imports:
                to_parts = imp.module.split("/") if "/" in imp.module else imp.module.split(".")
                to_mod = to_parts[0] if to_parts else imp.module

                if to_mod and to_mod != from_mod and not imp.is_relative:
                    pair_counts[(from_mod, to_mod)] += 1

        couplings: list[ModuleCoupling] = []
        for (from_m, to_m), count in sorted(pair_counts.items(), key=lambda x: -x[1]):
            couplings.append(
                ModuleCoupling(
                    from_module=from_m,
                    to_module=to_m,
                    import_count=count,
                    relationship="static_import",
                    confidence="high",
                )
            )
        return couplings
