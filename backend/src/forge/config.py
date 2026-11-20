import ipaddress
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_BACKEND_ROOT = Path(__file__).resolve().parents[2]


class ForgeSettings(BaseSettings):
    environment: Literal["development", "test", "production"] = "development"
    gemini_api_key: str | None = None
    openrouter_api_key: str | None = None
    ollama_host: str = "http://127.0.0.1:11434"
    default_model: str = "ollama"
    cors_origins: str = "http://localhost:5173"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    model_config = SettingsConfigDict(
        env_file=_BACKEND_ROOT / ".env.local",
        extra="ignore",
        validate_default=True,
    )

    @model_validator(mode="after")
    def validate_cors_origins(self) -> "ForgeSettings":
        origins = [origin.strip().rstrip("/") for origin in self.cors_origins.split(",")]
        if not origins or any(not origin for origin in origins):
            raise ValueError("CORS_ORIGINS must contain at least one valid origin")

        normalized_origins: list[str] = []
        for origin in origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
                or "*" in origin
            ):
                raise ValueError(f"Invalid CORS origin: {origin!r}")

            try:
                parsed.port
            except ValueError as exc:
                raise ValueError(f"Invalid CORS origin port: {origin!r}") from exc

            normalized_origins.append(origin)

            if self.environment == "production":
                try:
                    loopback = ipaddress.ip_address(parsed.hostname).is_loopback
                except ValueError:
                    loopback = parsed.hostname.lower() == "localhost"
                if parsed.scheme != "https" or loopback:
                    raise ValueError(
                        "Production CORS origins must use HTTPS and cannot target localhost"
                    )

        self.cors_origins = ",".join(dict.fromkeys(normalized_origins))
        return self

    @property
    def allowed_origins(self) -> list[str]:
        return self.cors_origins.split(",")


@lru_cache
def get_settings() -> ForgeSettings:
    return ForgeSettings()


def forge_directory(workspace: Path) -> Path:
    return workspace / ".forge"


def instructions_path(workspace: Path) -> Path:
    return workspace / "FORGE.md"
