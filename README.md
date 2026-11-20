# Forge

**Your code. Your models. Your agent.**

Forge is an early-stage, model-agnostic coding agent project with a Python backend, a separate React web frontend, and a terminal CLI. The web dashboard currently reports API and provider configuration status; the agent runtime and interactive coding workflow are not implemented yet.

## Project layout

```text
forge/
├── backend/                 # Python API, CLI, shared application code, tests
│   ├── src/forge/
│   │   ├── api/              # FastAPI application and HTTP routes
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
- An optional model provider: Ollama, Google Gemini, or OpenRouter

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
uv run --project backend forge diff .
```

The prompt runner is not implemented yet; `forge -p ...` currently reports that limitation rather than sending a request.

## API endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/health` | Check whether the API is responding |
| `GET` | `/api/v1/providers` | Read the default model and provider configuration status |

These are initial integration endpoints, not the agent execution API.

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
- The API's development CORS allowlist is configured with `FORGE_CORS_ORIGINS`.
- Workspace permissions, command execution controls, and model-generated tool safety are required before the agent runtime can execute changes. See [Implementation status and steps](docs/IMPLEMENTATION.md).

## License

MIT; see [LICENSE](LICENSE).
