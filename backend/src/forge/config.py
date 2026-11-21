import ipaddress
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ROOT = Path(__file__).resolve().parents[2]


class ForgeSettings(BaseSettings):
    environment: Literal["development", "test", "production"] = "development"
    forge_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    ollama_host: str = "http://127.0.0.1:11434"
    default_model: str = "ollama"
    cors_origins: str = "http://localhost:5173"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    model_config = SettingsConfigDict(
        env_file=_BACKEND_ROOT / ".env.local",
        extra="ignore",
        validate_default=True,
    )

    @field_validator("forge_api_key", "gemini_api_key", "openrouter_api_key", mode="before")
    @classmethod
    def empty_api_key_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("ollama_host")
    @classmethod
    def validate_ollama_host(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return value

        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError("OLLAMA_HOST must be an HTTP(S) origin without credentials or a path")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("OLLAMA_HOST contains an invalid port") from exc
        return value

    @model_validator(mode="after")
    def validate_cors_origins(self) -> "ForgeSettings":
        if self.forge_api_key is not None:
            if len(self.forge_api_key.get_secret_value()) < 32:
                raise ValueError("FORGE_API_KEY must contain at least 32 characters")
        elif self.environment == "production":
            raise ValueError("Production requires FORGE_API_KEY with at least 32 characters")

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
