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
│   │   ├── context/          # Workspace discovery, search, and guarded writes
│   │   ├── execution/        # Explicitly approved development commands
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
- Ollama for local inference, or Gemini/OpenRouter credentials for cloud inference

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
uv run --project backend forge run --workspace . -- python --version
uv run --project backend forge diff .
```

`forge models` checks configured providers and lists available models. Set `OLLAMA_HOST` in `backend/.env.local` if Ollama is not using its default local address.

`forge files`, `forge read`, and `forge search` provide bounded access to the selected workspace. `forge write` shows a diff and requires explicit confirmation before atomically creating or replacing a file. It refuses sensitive paths, symlinks, parent traversal, and paths outside the workspace. Do not pass secrets as command-line content because shells may retain command history.

`forge run` allows only `git`, `npm`, `node`, `python`, `python3`, `pytest`, `ruff`, and `uv`. It displays the resolved executable, exact arguments, working directory, and timeout, then requires confirmation (default: no) for each invocation. It does not invoke a shell; execution has a timeout and bounded output, and only a small environment-variable allowlist is inherited. This is an approval gate, not an OS sandbox: an approved program runs as the current user and can access resources permitted to that account.

Ollama, Gemini, and OpenRouter support bounded, non-streaming text generation through the backend provider interface and authenticated API. Set `DEFAULT_MODEL` to `ollama`, `gemini`, or `openrouter` to select the generation provider. The prompt runner and agent loop are not wired up yet; `forge -p ...` reports that limitation rather than sending a request.

## API endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/health` | Check whether the API is responding |
| `GET` | `/api/v1/providers` | Read the default model and provider configuration status |
| `POST` | `/api/v1/generate` | Generate a bounded response using the configured provider |

Generation requires `Authorization: Bearer <FORGE_API_KEY>`. Set `FORGE_API_KEY` to a random value of at least 32 characters in `backend/.env.local`; production settings require it. The endpoint accepts at most 64 messages (up to 64,000 characters each) and limits generated output to 8,192 tokens. It does not expose workspace access or command execution.

Example request body:

```json
{
  "model": "qwen2.5-coder:7b",
  "messages": [{"role": "user", "content": "Explain this code"}],
  "max_tokens": 1024
}
```

These are initial integration endpoints, not the agent execution API.

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

`FORGE_API_KEY` must be at least 32 characters when set. Generate one with `uv run --project backend python -c "import secrets; print(secrets.token_urlsafe(32))"` and keep it server-side. For production, set `ENVIRONMENT=production`, configure `FORGE_API_KEY`, and explicitly configure HTTPS origins in `CORS_ORIGINS`. The backend rejects wildcard, malformed, and localhost production origins. The development defaults are intended only for local use.

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
- Command execution requires explicit per-command approval and is not exposed through the API. User authentication and model-generated tool safety are still required before remote agent workflows are exposed; the generation API key is not user identity. See [Implementation status and steps](docs/IMPLEMENTATION.md).

## License

MIT; see [LICENSE](LICENSE).
