from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from forge.api.routes import health, providers
from forge.config import ForgeSettings

settings = ForgeSettings()

app = FastAPI(
    title="Forge API",
    description="HTTP API for the Forge coding agent.",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(health.router, prefix="/api/v1")
app.include_router(providers.router, prefix="/api/v1")
