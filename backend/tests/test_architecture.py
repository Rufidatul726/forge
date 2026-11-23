from pathlib import Path

from forge.intelligence.architecture import ArchitectureAnalyzer
from forge.intelligence.models import (
    ExtractedFileSymbols,
    ImportRecord,
    ProjectManifest,
    RepoInventory,
    ScannedFile,
)


def test_architecture_analyzer():
    manifests = [
        ProjectManifest(
            ecosystem="python",
            manifest_path="backend/pyproject.toml",
            name="backend-app",
            dependencies=["fastapi", "sqlalchemy"],
            dev_dependencies=["pytest"],
            frameworks=["fastapi", "sqlalchemy", "pytest"],
        ),
        ProjectManifest(
            ecosystem="node",
            manifest_path="frontend/package.json",
            name="frontend-app",
            dependencies=["react"],
            dev_dependencies=["vite", "vitest"],
            frameworks=["react", "vite", "vitest"],
        ),
    ]

    files = [
        ScannedFile("backend/src/main.py", "python", 500, "hash1", 100.0),
        ScannedFile("backend/src/models.py", "python", 300, "hash2", 100.0),
        ScannedFile("backend/tests/test_main.py", "python", 400, "hash3", 100.0, is_test=True),
        ScannedFile("frontend/src/App.test.tsx", "tsx", 400, "hash4", 100.0, is_test=True),
    ]

    inventory = RepoInventory(
        workspace_root="/test",
        files=files,
        directories=["backend", "backend/src", "backend/tests", "backend/alembic", "frontend", "frontend/src"],
        languages={"python": 3, "tsx": 1},
        manifests=manifests,
    )

    file_contents = {
        "backend/src/main.py": """
from fastapi import FastAPI, APIRouter
app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/items")
def create_item():
    pass
""",
        "backend/src/models.py": """
from sqlalchemy.orm import declarative_base
Base = declarative_base()

class UserModel(Base):
    pass
""",
    }

    parsed_files = {
        "backend/src/main.py": ExtractedFileSymbols(
            path="backend/src/main.py",
            imports=[ImportRecord("backend/src/main.py", "fastapi", ["FastAPI", "APIRouter"])],
        ),
    }

    analyzer = ArchitectureAnalyzer()
    summary = analyzer.analyze(inventory, parsed_files, file_contents)

    # Check boundaries
    boundary_names = [b.name for b in summary.boundaries]
    assert "backend" in boundary_names
    assert "frontend" in boundary_names

    # Check API routes
    routes = {(r.method, r.path) for r in summary.api_routes}
    assert ("GET", "/health") in routes
    assert ("POST", "/items") in routes
    health_route = next(r for r in summary.api_routes if r.path == "/health")
    assert health_route.handler_symbol == "health"

    # Check DB models
    assert len(summary.database_models) == 1
    assert summary.database_models[0].name == "UserModel"
    assert summary.database_models[0].framework == "sqlalchemy"

    # Check migration directories
    assert "backend/alembic" in summary.migration_directories

    # Check test suites
    frameworks = [ts.framework for ts in summary.test_suites]
    assert "pytest" in frameworks
    assert "vitest/jest" in frameworks
