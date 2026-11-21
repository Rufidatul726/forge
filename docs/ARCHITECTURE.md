# Forge architecture

## Repository boundaries

Forge is a small monorepo with independently managed Python and JavaScript applications:

- `backend/` owns the FastAPI HTTP API, Typer CLI, shared Python configuration, and Python tests.
- `frontend/` owns the React browser application and its Node dependencies.
- `docs/` owns project-level architecture, setup, and implementation status.

The backend is a Python package under `backend/src/forge`. The frontend never imports Python code; it communicates with the backend over the versioned HTTP API. The CLI remains a separate entry point and is not coupled to the frontend.

## Runtime shape

```text
Browser
  └── React + TypeScript + Vite (frontend/)
        └── HTTP / JSON, /api/v1
              └── FastAPI (backend/src/forge/api/)
                    ├── Routes
                    └── Settings (backend/.env.local)

Terminal
  └── Typer CLI (backend/src/forge/cli.py)
        └── Shared Python package (backend/src/forge/)
```

The API owns browser-facing contracts and CORS. Provider/model execution, repository context, tools, safety policy, and sessions belong in backend modules; they must not be reimplemented in the frontend.

## Model provider boundary

`backend/src/forge/providers/protocol.py` defines the async `ModelProvider` interface. Provider-neutral chat messages and bounded generation options live in `providers/models.py`; adapters map those models to provider-specific protocols and raise typed errors with safe public messages.

The first adapter is `OllamaProvider`:

- It reads the server-only `OLLAMA_HOST` setting and uses `/api/tags` to list installed models and `/api/chat` for non-streaming generation.
- Requests have a 30-second timeout, generation is capped at 8,192 tokens, and response bodies are limited to 2 MiB.
- Network, timeout, HTTP, malformed response, and oversized response failures map to typed provider errors without returning upstream response bodies.
- Cancellation is not swallowed.
- `forge models` exercises model discovery. Generation is available to backend callers but is not yet exposed through an API route or wired into the agent/CLI prompt workflow.

Gemini and OpenRouter credentials may be configured but their adapters are not implemented. Provider API keys must remain server-side and must not be included in logs or frontend responses.

## Read-only workspace context

`backend/src/forge/context/workspace.py` provides bounded workspace file discovery, UTF-8 reads, text search, and optional `FORGE.md` instruction loading. Operations accept workspace-relative paths and patterns, reject traversal and absolute paths, resolve symlinks before access, and exclude common generated directories and credential-bearing files. Reads and scans have byte, result, and candidate limits. Binary and oversized files are skipped in searches and reported in the result metadata.

The CLI exposes these capabilities as `forge files`, `forge read`, and `forge search`. The core is read-only: file writes and subprocess execution are not implemented. Workspace paths are an access boundary for normal use, not a sandbox against malicious code already running with the same OS account.

## Current API contract

### `GET /api/v1/health`

Returns `{"status":"ok","service":"forge-api"}` when the API process responds.

### `GET /api/v1/providers`

Returns the configured default model and whether supported provider settings are present:

```json
{
  "default_model": "ollama",
  "providers": [
    {"name": "ollama", "kind": "local", "configured": true},
    {"name": "gemini", "kind": "cloud", "configured": false},
    {"name": "openrouter", "kind": "cloud", "configured": false}
  ]
}
```

The Ollama `configured` value indicates that an endpoint is configured; it does not prove a model is installed or that the endpoint is reachable. These initial routes do not execute agent tasks.

## Error and request tracing contract

All responses include a server-generated `X-Request-ID`. The ID is also present in request-completion logs and in the JSON body for errors. Validation errors include field locations, error messages, and types, but never echo submitted values. Unexpected errors return a generic message; logs record the exception type without exception text.

Errors use this shape:

```json
{
  "error": {"code": "validation_error", "message": "Request validation failed."},
  "request_id": "...",
  "details": [
    {"location": "path.item_id", "message": "Input should be a valid integer", "type": "int_parsing"}
  ]
}
```

`details` is omitted when it does not apply. The OpenAPI document references the shared error schema for route error responses.

## Configuration and trust boundaries

- Backend settings are loaded from `backend/.env.local` and normal process environment variables. Environment variables take precedence.
- Frontend settings are loaded by Vite from `frontend/.env.local`.
- `VITE_*` values are included in browser code and are public. Provider secrets belong only in the backend.
- `OLLAMA_HOST` is validated as an HTTP(S) origin; embedded credentials, paths, and query parameters are rejected.
- `CORS_ORIGINS` is a comma-separated allowlist. Production requires explicit HTTPS origins and rejects wildcard and localhost origins.
- `ENVIRONMENT` selects `development`, `test`, or `production`; `LOG_LEVEL` selects the backend log level.
- The API allows only the methods and headers currently needed by the dashboard and does not enable credentialed CORS.
- The UI displays backend connection failures; it does not treat an unavailable API as a successful response.
- Workspace access and generated command/file operations must be permission-gated before execution support is added.

## Extension conventions

- Keep route definitions in `backend/src/forge/api/routes/`; register them in `api/main.py`.
- Keep API request and response schemas explicit and version routes under `/api/v1`.
- Put domain behavior in backend services rather than route functions or React components.
- Add backend tests for new API/service behavior and frontend tests for user-facing state transitions as those capabilities are introduced.
- Update this document when process boundaries or API contracts change.
