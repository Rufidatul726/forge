# Forge

**Your code. Your models. Your agent.**

Forge is an early-stage, model-agnostic coding agent project with a Python backend, a separate React web frontend, and a terminal CLI. The web dashboard currently reports API and provider configuration status; the agent runtime and interactive coding workflow are not implemented yet.

## Project layout

```text
forge/
├── backend/                 # Python API, CLI, shared application code, tests
│   ├── src/forge/
│   │   ├── api/              # FastAPI application and HTTP routes
│   │   ├── providers/        # Provider interface and model adapters
│   │   ├── context/          # Read-only workspace discovery and search
│   │   ├── cli.py            # Typer terminal interface
│   │   └── config.py         # Environment-backed settings
│   ├── tests/
│   └── pyproject.toml
├── frontend/                # React + TypeScript + Vite web application
│   ├── src/
│   └── package.json
├── docs/                    # Architecture, development, and implementation status
├── .gitignore
└── README.md
```

See [Architecture](docs/ARCHITECTURE.md), [Development](docs/DEVELOPMENT.md), and [Implementation status and steps](docs/IMPLEMENTATION.md) for design and project status.

## Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- Node.js 20.19+ and npm
- Git
- Ollama for local inference (Gemini and OpenRouter adapters are not implemented yet)

## Set up the environment

From the repository root, copy each example file to its local counterpart:

```powershell
Copy-Item backend\.env.example backend\.env.local
Copy-Item frontend\.env.example frontend\.env.local
```

The `.env.local` files are intentionally ignored by Git. Add provider API keys to `backend/.env.local`; never put secrets in frontend variables. Only variables prefixed with `VITE_` are intended for the browser.

### Start the backend

In one terminal:

```powershell
uv sync --project backend --extra dev
uv run --project backend uvicorn forge.api.main:app --reload
```

The API listens on `http://127.0.0.1:8000`. Interactive API documentation is at `http://127.0.0.1:8000/docs`.

### Start the frontend

In a second terminal:

```powershell
Set-Location frontend
npm install
npm run dev
```

Open the local URL printed by Vite, normally `http://localhost:5173`.

### Use the CLI

From the repository root:

```powershell
uv run --project backend forge --help
uv run --project backend forge init .
uv run --project backend forge models
uv run --project backend forge files .
uv run --project backend forge read README.md
uv run --project backend forge search "TODO"
uv run --project backend forge write src/example.py --content "print('hello')"
uv run --project backend forge diff .
```

`forge models` checks the configured Ollama service and lists its installed models. Set `OLLAMA_HOST` in `backend/.env.local` if Ollama is not using its default local address.

`forge files`, `forge read`, and `forge search` provide bounded access to the selected workspace. `forge write` shows a diff and requires explicit confirmation before atomically creating or replacing a file. It refuses sensitive paths, symlinks, parent traversal, and paths outside the workspace. Do not pass secrets as command-line content because shells may retain command history.

The Ollama provider supports bounded, non-streaming text generation through the backend provider interface. The prompt runner and agent loop are not wired up yet; `forge -p ...` reports that limitation rather than sending a request. Gemini and OpenRouter are configuration placeholders until their adapters are implemented.

## API endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/health` | Check whether the API is responding |
| `GET` | `/api/v1/providers` | Read the default model and provider configuration status |

These are initial integration endpoints, not the agent execution API. Provider inference is not exposed as an HTTP endpoint yet.

Every API response includes an `X-Request-ID` header. Errors use a consistent JSON contract:

```json
{
  "error": {"code": "not_found", "message": "Resource not found."},
  "request_id": "..."
}
```

Validation errors may include sanitized field locations and messages. Unexpected errors return a generic message; logs include the request ID, method, path, status, duration, and exception type without echoing exception text.

## Backend configuration

`backend/.env.local` accepts `ENVIRONMENT` (`development`, `test`, or `production`), provider settings, `DEFAULT_MODEL`, `CORS_ORIGINS`, and `LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING`, or `ERROR`). Environment variables override the local file.

For production, set `ENVIRONMENT=production` and explicitly configure HTTPS origins in `CORS_ORIGINS`. The backend rejects wildcard, malformed, and localhost production origins. The development defaults are intended only for local use.

## Validation

```powershell
# Backend tests
uv run --project backend --extra dev pytest backend\tests

# Backend package build
uv build --project backend

# Frontend production build
Set-Location frontend
npm run build
npm run lint
```

Additional setup and troubleshooting notes are in [Development](docs/DEVELOPMENT.md).

## Security and configuration

- Keep credentials in `backend/.env.local` or in the process environment.
- Do not use secrets in `VITE_*` variables; frontend build-time variables are public.
- The API's development CORS allowlist is configured with `CORS_ORIGINS`.
- Workspace permissions, command execution controls, and model-generated tool safety are required before the agent runtime can execute changes. See [Implementation status and steps](docs/IMPLEMENTATION.md).

## License

MIT; see [LICENSE](LICENSE).
