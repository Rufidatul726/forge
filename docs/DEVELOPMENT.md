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

Production mode requires `CORS_ORIGINS` to be explicitly changed to one or more HTTPS origins. Wildcards and localhost origins are rejected. Keep the local development settings for local use only.

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
```

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
