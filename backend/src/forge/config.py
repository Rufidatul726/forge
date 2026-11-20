from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


_BACKEND_ROOT = Path(__file__).resolve().parents[2]


class ForgeSettings(BaseSettings):
    gemini_api_key: str | None = None
    openrouter_api_key: str | None = None
    ollama_host: str = "http://127.0.0.1:11434"
    default_model: str = "ollama"
    cors_origins: str = "http://localhost:5173"

    model_config = SettingsConfigDict(env_file=_BACKEND_ROOT / ".env.local", extra="ignore")


def forge_directory(workspace: Path) -> Path:
    return workspace / ".forge"


def instructions_path(workspace: Path) -> Path:
    return workspace / "FORGE.md"
