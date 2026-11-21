# Development guide

## Prerequisites

- Python 3.12+
- uv
- Node.js 20.19+ and npm
- Git

## Local configuration

From the repository root:

```powershell
Copy-Item backend\.env.example backend\.env.local
Copy-Item frontend\.env.example frontend\.env.local
```

`backend/.env.local` is for server-only settings and provider credentials. `frontend/.env.local` contains browser-visible Vite settings only. Both local files are ignored by Git.

Backend environment variables:

| Variable | Default | Description |
|---|---|---|
| `ENVIRONMENT` | `development` | Runtime environment: `development`, `test`, or `production` |
| `FORGE_API_KEY` | empty | Bearer key for `POST /api/v1/generate`; at least 32 characters when set and required in production |
| `GEMINI_API_KEY` | empty | Gemini credential; not used by an inference adapter yet |
| `OPENROUTER_API_KEY` | empty | OpenRouter credential; not used by an inference adapter yet |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Ollama endpoint |
| `DEFAULT_MODEL` | `ollama` | Default model label returned by the API |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated allowed browser origins |
| `LOG_LEVEL` | `INFO` | Backend log level: `DEBUG`, `INFO`, `WARNING`, or `ERROR` |

Frontend environment variables:

| Variable | Default | Description |
|---|---|---|
| `VITE_API_BASE_URL` | `http://127.0.0.1:8000/api/v1` | Versioned backend API base URL |

Generate an API key with `uv run --project backend python -c "import secrets; print(secrets.token_urlsafe(32))"`. Store it in `backend/.env.local`; do not commit it or put it in a `VITE_*` setting. The generation API requires this key in an `Authorization: Bearer ...` header and returns `503` until configured. Production mode requires both `FORGE_API_KEY` and `CORS_ORIGINS` with one or more HTTPS origins. Wildcards and localhost origins are rejected. Keep the local development settings for local use only.

`OLLAMA_HOST` must be an HTTP(S) origin with no embedded credentials, URL path, or query string. To inspect the service and installed models:

```powershell
uv run --project backend forge models
```

The Ollama adapter uses a 30-second timeout, limits response bodies to 2 MiB, and caps each generation request at 8,192 tokens. Authenticated generation is available at `POST /api/v1/generate`; the CLI prompt workflow and agent loop are not connected to it yet.

## Run locally

Run the backend in one terminal from the repository root:

```powershell
uv sync --project backend --extra dev
uv run --project backend uvicorn forge.api.main:app --reload
```

Run the frontend in another terminal:

```powershell
Set-Location frontend
npm install
npm run dev
```

The API is available at `http://127.0.0.1:8000`; its Swagger UI is at `http://127.0.0.1:8000/docs`. Vite prints the web URL after startup.

Run the CLI from the repository root:

```powershell
uv run --project backend forge --help
uv run --project backend forge files .
uv run --project backend forge read README.md
uv run --project backend forge search "TODO"
uv run --project backend forge write src/example.py --content "print('hello')"
uv run --project backend forge run --workspace . -- python --version
```

Workspace file operations reject absolute paths, parent traversal, escaped symlinks, sensitive files, and common generated directories. Reads and writes are capped at 1 MiB per file; searches also cap candidate files and result lines. `forge write` previews a diff and asks for confirmation before replacing or creating one file. It does not create parent directories. Avoid putting secrets in `--content`, which can be retained in shell history. Use a narrower `--pattern` for large repositories.

`forge run` permits only `git`, `npm`, `node`, `python`, `python3`, `pytest`, `ruff`, and `uv`. Pass `--` before the command to separate Forge options from the command arguments. Forge previews the resolved executable and full argument vector, then asks for per-command approval with a default of no. The process runs without a shell, is limited to 300 seconds and 1 MiB of captured output per stream by default, and receives only an environment-variable allowlist. Cancellation and timeout cleanup use process groups on POSIX and `taskkill /T /F` on Windows, with a parent-process fallback. These controls are not an OS sandbox; approved programs run as your current user and may access resources available to that account. The command runner is not available through the API.

## Tests and builds

From the repository root:

```powershell
uv run --project backend --extra dev pytest backend\tests
uv build --project backend
```

From `frontend/`:

```powershell
npm run lint
npm run build
```

## Troubleshooting

- **Frontend reports API offline:** Start the backend and check `VITE_API_BASE_URL` and `CORS_ORIGINS`. The frontend dev origin must be in the backend CORS list.
- **Provider secrets do not show as configured:** Put them in `backend/.env.local`, restart the backend, and do not prefix backend secrets with `VITE_`.
- **Missing Python dependencies:** Run `uv sync --project backend --extra dev` from the repository root.
- **Missing frontend dependencies:** Run `npm install` from `frontend/`.
