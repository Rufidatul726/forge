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
| `GEMINI_API_KEY` | empty | Gemini credential for the Gemini REST adapter |
| `OPENROUTER_API_KEY` | empty | OpenRouter credential for the OpenRouter REST adapter |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Ollama endpoint |
| `DEFAULT_MODEL` | `ollama` | Generation provider: `ollama`, `gemini`, or `openrouter` |
| `AGENT_WORKSPACE_ROOT` | empty | Required server-side workspace root for read-only `POST /api/v1/agent/tasks` |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated allowed browser origins |
| `LOG_LEVEL` | `INFO` | Backend log level: `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `INSTANCE_ID` | auto-generated | Unique node identifier for distributed clusters |
| `LOG_FORMAT` | `text` | Log format: `text` or `json` for structured log aggregators |
| `METRICS_ENABLED` | `true` | Whether Prometheus metrics scraping is enabled at `GET /api/v1/metrics` |
| `RATE_LIMIT_REQUESTS` | `60` | Maximum requests from one direct client in each rate-limit window for authenticated generation, task, and session routes |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Positive length of the process-local rate-limit window in seconds |
| `BRAVE_SEARCH_API_KEY` | empty | API key for the official Brave Search REST adapter (free tier 2,000 queries/month) |
| `TAVILY_API_KEY` | empty | API key for the Tavily Search API adapter (free tier 1,000 queries/month) |
| `SEARXNG_URL` | empty | Base URL of a self-hosted or public SearXNG instance (no API key required) |
| `SEARCH_PROVIDER` | `auto` | Search provider choice: `auto`, `brave`, `tavily`, `searxng`, or `mock` |
| `RESEARCH_CACHE_TTL_DAYS` | `7` | Cache expiration time in days for retrieved documentation |
| `RESEARCH_MAX_CACHE_DOCUMENTS` | `500` | Maximum number of documents retained in `.forge/research.sqlite3` |
| `RESEARCH_MAX_CACHE_BYTES` | `52428800` | Maximum byte capacity (default 50 MiB) for the research cache before eviction |

Frontend environment variables:

| Variable | Default | Description |
|---|---|---|
| `VITE_API_BASE_URL` | `http://127.0.0.1:8000/api/v1` | Versioned backend API base URL |
| `VITE_FORGE_API_KEY` | empty | Optional local-development API key; visible to the browser |

Generate an API key with `uv run --project backend python -c "import secrets; print(secrets.token_urlsafe(32))"`. Store it in `backend/.env.local`; do not commit it or put it in a `VITE_*` setting. The generation API requires this key in an `Authorization: Bearer ...` header and returns `503` until configured. Production mode requires both `FORGE_API_KEY` and `CORS_ORIGINS` with one or more HTTPS origins. Wildcards and localhost origins are rejected. Keep the local development settings for local use only.

The built-in rate limiter is a process-local sliding window keyed by the direct client address; it intentionally does not trust `X-Forwarded-For`. When deploying multiple API instances or putting Forge behind a reverse proxy, enforce an equivalent shared limit at the edge and configure the proxy/client-address chain there. Keep `backend/.env.local` out of images and source control; inject provider credentials and `FORGE_API_KEY` through the deployment platform’s secret store, rotate them on exposure, and grant only the runtime identity access to them.

`OLLAMA_HOST` must be an HTTP(S) origin with no embedded credentials, URL path, or query string. Set `DEFAULT_MODEL` to `ollama`, `gemini`, or `openrouter` to select the generation provider. To inspect configured providers and their available models:

```powershell
uv run --project backend forge models
```

All provider adapters use a 30-second timeout, limit response bodies to 2 MiB, and cap each generation request at 8,192 tokens. Authenticated generation is available at `POST /api/v1/generate`.

To enable read-only API task workflows, set `AGENT_WORKSPACE_ROOT` to the repository directory on the server. The API supports both synchronous tasks (`POST /api/v1/agent/tasks`) and real-time Server-Sent Events streaming (`POST /api/v1/agent/tasks/stream`). The frontend provides a "Stream" toggle to stream responses with live typing indicators. Requests can include up to 10 safe, workspace-relative context paths; they cannot write files or execute commands.

Sessions use SQLite at `<AGENT_WORKSPACE_ROOT>/.forge/sessions.sqlite3`; no additional database service or environment variable is required. Use `forge sessions create --workspace .`, `forge sessions list --workspace .`, `forge sessions show <id> --workspace .`, and `forge sessions delete <id> --workspace .` to manage the same sessions used by the API. The delete command asks for confirmation.

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
uv run --project backend forge symbols "Calculator"
uv run --project backend forge write src/example.py --content "print('hello')"
uv run --project backend forge run --workspace . -- python --version
uv run --project backend forge diff .
uv run --project backend forge checkpoints
uv run --project backend forge rollback
```

Workspace file operations reject absolute paths, parent traversal, escaped symlinks, sensitive files, and common generated directories. Reads and writes are capped at 1 MiB per file; searches also cap candidate files and result lines. `forge write` previews a diff and asks for confirmation before replacing or creating one file. It snapshots pre-change file states via `CheckpointManager` so tasks can be safely rolled back using `forge rollback`. Rollback detects conflicts when files were modified after the task and will never overwrite unrelated user edits.

`forge symbols` scans code files across the repository extracting functions, classes, methods, and interfaces without external language servers. It leverages the persistent SQLite index when present, falling back to workspace regex scanning.

`forge index` builds an incremental, persistent SQLite index in `<workspace>/.forge/index.sqlite3`.
- `forge index`: Runs incremental indexing, computing content SHA-256 hashes and updating only modified or added files.
- `forge index --status`: Reports indexed file counts, total symbols, language breakdown, and last indexing run stats.
- `forge index --force`: Triggers a clean rebuild from scratch.

`forge map` prints a deterministic repository map with directory hierarchy, summaries, and detected project boundaries.

`forge references <symbol>` lists references to a symbol across the repository, distinguishing resolved symbol targets from unresolved/textual references.

`forge research` provides web research, documentation retrieval, and evidence grounding:
- `forge research "query"`: Submits a query to the configured search provider, retrieves and extracts documentation pages, deterministically ranks them, and displays verified facts, excerpts, and citations.
- `forge research "query" --domain <domain>`: Restricts search to specified documentation domain(s).
- `forge research fetch <url>`: Safely fetches and parses a documentation URL using bounded HTTP retrieval with SSRF and DNS rebinding protections.
- `forge research show <id>`: Displays full details of a persisted research report.
- `forge research sources <id>`: Lists ranked sources and explainable scoring signals for a research report.
- `forge research cache --status`: Reports persistent document cache metrics, document counts, and capacity.
- `forge research cache --clear`: Prunes all cached documentation entries.

`forge explain <path>` explains a file by listing its defined symbols, imports, incoming caller references, and associated test files.

`forge context "<task>"` tests task-oriented context retrieval, outputting an explainable manifest of included/excluded files and token usage.

### Repository intelligence specifications & limitations

- **Supported languages**:
  - Python (`.py`, `.pyi`): Full AST parsing extracting functions, classes, methods, docstrings, signatures, constants, imports, and reference calls.
  - TypeScript & JavaScript (`.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs`): Structural and lexical parsing extracting classes, methods, functions, interfaces, type aliases, constants, ES6/CommonJS imports, and references.
- **Unsupported analysis cases**:
  - Dynamic runtime metaprogramming, `eval`, `getattr`, `importlib`, and monkey-patching cannot be fully resolved statically and are marked as unresolved/textual matches.
  - Cross-file type inference without compilation artifacts.
- **Indexing limits**:
  - Maximum 5,000 files per workspace scan (`max_files`).
  - Maximum 1 MiB per file (`max_file_bytes`). Larger files have bounded hash computation and are omitted from AST parsing.
  - Excluded directories (`node_modules`, `.git`, `.forge`, `.venv`, cache dirs) and sensitive files (`.env`, secrets, private keys) are never indexed.
- **Retrieval limitations**:
  - Token budget enforcement is strictly applied with conservative token estimation (`~4 chars/token`). Lower-priority files are truncated or excluded when the budget ceiling is reached.

`forge -p "..." --agent` enables the autonomous coding agent loop:
- In `normal` mode (default), read operations execute automatically while writes and allowlisted commands require interactive user confirmation.
- In `plan` mode (`--plan --agent` or `--policy plan`), read-only actions are permitted and mutations or commands are strictly blocked by policy.
- In `autonomous` mode (`--policy autonomous`), actions in `--allow-tool` or configured in `~/.forge/config.json` / `.forge/config.json` run without prompting. Autonomous permissions are disabled by default.

`forge run` permits only `git`, `npm`, `node`, `python`, `python3`, `pytest`, `ruff`, and `uv`. Pass `--` before the command to separate Forge options from the command arguments. Forge previews the resolved executable and full argument vector, then asks for per-command approval with a default of no. The process runs without a shell, is limited to 300 seconds and 1 MiB of captured output per stream by default, and receives only an environment-variable allowlist. Cancellation and timeout cleanup use process groups on POSIX and `taskkill /T /F` on Windows, with a parent-process fallback. These controls are not an OS sandbox; approved programs run as your current user and may access resources available to that account. The command runner is not available through the API.

## Tests and builds

From the repository root:

```powershell
uv run --project backend --extra dev pytest backend\tests
uv build --project backend
```

From `frontend/`:

```powershell
npm run test
npm run lint
npm run build
```

The CI workflow runs the same backend tests/build and frontend tests/lint/build
on pushes and pull requests.

## Troubleshooting

- **Frontend reports API offline:** Start the backend and check `VITE_API_BASE_URL` and `CORS_ORIGINS`. The frontend dev origin must be in the backend CORS list.
- **Provider secrets do not show as configured:** Put them in `backend/.env.local`, restart the backend, and do not prefix backend secrets with `VITE_`.
- **Missing Python dependencies:** Run `uv sync --project backend --extra dev` from the repository root.
- **Missing frontend dependencies:** Run `npm install` from `frontend/`.
