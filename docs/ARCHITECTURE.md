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
