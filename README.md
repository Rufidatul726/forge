# Forge

**Your code. Your models. Your agent.**

Forge is an early-stage, model-agnostic coding agent project with a Python backend, a separate React web frontend, and a terminal CLI. The dashboard reports API/provider status, and the frontend provides a read-only chat workflow with real-time SSE streaming backed by persistent sessions.

## Project layout

```text
forge/
├── backend/                 # Python API, CLI, shared application code, tests
│   ├── src/forge/
│   │   ├── api/              # FastAPI application and HTTP routes
│   │   ├── providers/        # Provider interface and model adapters
│   │   ├── context/          # Workspace discovery, search, and guarded writes
│   │   ├── execution/        # Explicitly approved development commands
│   │   ├── intelligence/     # Repository scanning, AST parsing, symbol indexing
│   │   ├── research/         # Web research, safe URL retrieval, doc extraction, citations
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

For local chat and session routes, set `VITE_FORGE_API_KEY` or enter the
`FORGE_API_KEY` value in the frontend Settings view. Vite variables are public
browser configuration, so never use this approach for a shared or production
deployment.

### Use the CLI

From the repository root:

```powershell
uv run --project backend forge --help
uv run --project backend forge init .
uv run --project backend forge models
uv run --project backend forge files .
uv run --project backend forge read README.md
uv run --project backend forge search "TODO"
uv run --project backend forge symbols "User"
uv run --project backend forge write src/example.py --content "print('hello')"
uv run --project backend forge run --workspace . -- python --version
uv run --project backend forge diff .
uv run --project backend forge checkpoints
uv run --project backend forge rollback
uv run --project backend forge index
uv run --project backend forge index --status
uv run --project backend forge map
uv run --project backend forge references User
uv run --project backend forge explain src/example.py
uv run --project backend forge context "Add validation to user registration"
```

`forge models` checks configured providers and lists available models. Set `OLLAMA_HOST` in `backend/.env.local` if Ollama is not using its default local address.

`forge index` performs bounded, incremental repository indexing into a versioned SQLite database at `<workspace>/.forge/index.sqlite3`. It parses Python ASTs and TypeScript/JavaScript/JSX/TSX symbols, tracks imports, and resolves references without running code. Pass `--status` to view statistics and breakdown, or `--force` for a clean rebuild.

`forge map` generates a deterministic repository map with directory hierarchies, detected ecosystems (Python, Node), frameworks, and architectural boundaries.

`forge symbols <query>` searches functions, classes, methods, interfaces, and types using the persistent index (falling back to workspace regex search when unindexed).

`forge references <symbol>` inspects references across the codebase, distinguishing resolved symbol definitions from textual/unresolved references.

`forge explain <path>` inspects a file, summarizing its symbols, imports, incoming calls, and related test suites.

`forge context <task>` runs task-oriented context retrieval with explainable relevance scoring, keyword matching, test correlation, Git change boosting, and hard token-budget enforcement with manifest reporting.

`forge files`, `forge read`, and `forge search` provide bounded access to the selected workspace. `forge write` shows a diff and requires explicit confirmation before atomically creating or replacing a file. It refuses sensitive paths, symlinks, parent traversal, and paths outside the workspace. Pass `--session <32-character-id>` to record approved, hash-only workspace change metadata in an existing session. Do not pass secrets as command-line content because shells may retain command history.

`forge checkpoints` and `forge rollback` track file modifications per task and provide safe rollbacks. Checkpoint payloads are versioned with canonical SHA-256 integrity checksums to detect accidental corruption or tampering, and rollback inspects current file hashes against post-change states to never overwrite unrelated changes made by the user.

`forge run` allows only `git`, `npm`, `node`, `python`, `python3`, `pytest`, `ruff`, and `uv`. It displays the resolved executable, exact arguments, working directory, and timeout, then requires confirmation (default: no) for each invocation. It does not invoke a shell; execution has a timeout and bounded output, and only a small environment-variable allowlist is inherited. This is an approval gate, not an OS sandbox: an approved program runs as the current user and can access resources permitted to that account.

Ollama, Gemini, and OpenRouter support bounded text generation, native structured tool calling (including streaming structured tool calls), and real-time streaming through the backend provider interface and authenticated API. Set `DEFAULT_MODEL` to `ollama`, `gemini`, or `openrouter` to select the generation provider. `POST /api/v1/agent/tasks/stream` streams responses via Server-Sent Events, and the web UI supports real-time streaming responses with typing indicators. `forge -p ...` runs a bounded, read-only repository task and persists it to a local session.

Add `--agent` to enable a bounded CLI tool loop with native structured tool calling:
- **Normal mode** (default): reads (`read_file`, `search_text`, `search_symbols`, `list_files`) are automatic; file writes (`write_file`) and allowlisted command execution (`run_command`) require separate interactive approval for each action.
- **Plan mode** (`--plan --agent` or `--policy plan`): read-only; attempts to modify files or execute commands are strictly blocked by policy without prompting for approval.
- **Autonomous mode** (`--policy autonomous`): allows only explicitly permitted actions to execute without interactive approval (`--allow-tool <tool>`). Autonomous permissions are disabled by default and can be configured through global (`~/.forge/config.json`) and project (`.forge/config.json`) settings.

All tool calls are dispatched through `ToolRegistry` and strictly validated against parameter schemas (unexpected fields are forbidden). HTTP agent endpoints remain strictly read-only. Repository context selection allocates token budgets across nested `FORGE.md`/`AGENTS.md` instructions, file inventories, and requested context files while honoring `.gitignore` and sensitive file exclusions.

### Web research and evidence-grounded coding (Forge M4)

Forge provides a provider-neutral research subsystem for technical documentation retrieval, secure bounded fetching, explainable ranking, and evidence grounding:

```powershell
# Search technical topics across the web or specific documentation domains
uv run --project backend forge research "FastAPI dependency injection best practices"
uv run --project backend forge research "Pydantic v2 migration" --domain docs.pydantic.dev
uv run --project backend forge research "React Router loaders" --limit 5

# Safely retrieve and extract structured content from a documentation page
uv run --project backend forge research fetch https://fastapi.tiangolo.com/tutorial/security/

# Inspect stored research reports, evidence excerpts, and citations
uv run --project backend forge research show <research-id>
uv run --project backend forge research sources <research-id>

# Manage the persistent document cache
uv run --project backend forge research cache --status
uv run --project backend forge research cache --clear
```

- **Security & SSRF defense**: `SafeDocumentFetcher` enforces strict network boundaries: HTTP/HTTPS only, rejects private/loopback/link-local/multicast/reserved IPv4 & IPv6 destinations, eliminates DNS rebinding by pinning pre-validated IPs, limits redirects, validates content types, and bounds response streams (2 MiB limit).
- **Prompt-injection isolation**: External retrieved content is treated as untrusted data. External instructions cannot alter execution policies, system rules, or authorize writes/commands. Citations are strictly validated against verified source text.
- **Agent tools**: `web_search`, `fetch_url`, `search_documentation`, and `inspect_research` are available in `ToolRegistry` as read-only tools across normal, plan, and autonomous policies.

## API endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/health` | Check whether the API is responding |
| `GET` | `/api/v1/ready` | Check API key and workspace readiness |
| `GET` | `/api/v1/providers` | Read the default model and provider configuration status |
| `POST` | `/api/v1/research` | Submit a research query and generate an evidence-grounded report |
| `GET` | `/api/v1/research/{id}` | Inspect a previously generated research report by ID |
| `GET` | `/api/v1/research/{id}/sources` | Retrieve ranked sources and citations for a research report |
| `POST` | `/api/v1/generate` | Generate a bounded response using the configured provider |
| `POST` | `/api/v1/agent/tasks` | Run a read-only repository task in the configured workspace |
| `POST` | `/api/v1/agent/tasks/stream` | Stream a read-only repository task via Server-Sent Events |
| `POST` | `/api/v1/sessions` | Create a persistent workspace session |
| `GET` | `/api/v1/sessions` | List persistent workspace sessions |
| `GET` | `/api/v1/sessions/{session_id}` | Read a saved session, turns, and outcomes |
| `DELETE` | `/api/v1/sessions/{session_id}` | Delete one saved session |

Generation requires `Authorization: Bearer <FORGE_API_KEY>`. Set `FORGE_API_KEY` to a random value of at least 32 characters in `backend/.env.local`; production settings require it. The endpoint accepts at most 64 messages (up to 64,000 characters each) and limits generated output to 8,192 tokens. It does not expose workspace access or command execution.

For deployment behind an identity provider, set `AUTH_MODE=oidc` and
configure `OIDC_ISSUER` and `OIDC_AUDIENCE`. Forge verifies access tokens
against the issuer discovery document or `OIDC_JWKS_URL`, and optionally
requires `OIDC_REQUIRED_SCOPE` or `OIDC_REQUIRED_ROLE`. `api_key_or_oidc`
keeps the existing API-key mode available while also accepting OIDC tokens.

Example request body:

```json
{
  "model": "qwen2.5-coder:7b",
  "messages": [{"role": "user", "content": "Explain this code"}],
  "max_tokens": 1024
}
```

These are initial integration endpoints, not the agent execution API.

### Read-only agent tasks

`POST /api/v1/agent/tasks` requires the same bearer API key as generation and an explicit server-side `AGENT_WORKSPACE_ROOT`. The client cannot select the workspace root. It accepts a model, up to 32 chat messages, and up to 10 workspace-relative `context_paths`; Forge includes the bounded file list, optional `FORGE.md`, and requested readable files in the model context. It cannot write files, execute commands, create sessions, or stream responses.

```json
{
  "model": "qwen2.5-coder:7b",
  "messages": [{"role": "user", "content": "Explain main.py"}],
  "context_paths": ["main.py"]
}
```

### Persistent sessions

Sessions are stored in SQLite at `<AGENT_WORKSPACE_ROOT>/.forge/sessions.sqlite3`; that directory is excluded from workspace reads. Session routes require the same bearer key and use the configured workspace root. Create a session, provide its 32-character ID as `session_id` in later agent task requests, and Forge restores prior turns before the new message. Task responses and generation metadata are appended after a successful request. The CLI exposes the same store through `forge sessions create`, `list`, `show`, and `delete`; deletion requires confirmation.

Every API response includes an `X-Request-ID` header. Errors use a consistent JSON contract:

```json
{
  "error": {"code": "not_found", "message": "Resource not found."},
  "request_id": "..."
}
```

Validation errors may include sanitized field locations and messages. Unexpected errors return a generic message; logs include the request ID, method, path, status, duration, and exception type without echoing exception text.

## Backend configuration

`backend/.env.local` accepts `ENVIRONMENT` (`development`, `test`, or `production`), provider settings, `DEFAULT_MODEL`, `CORS_ORIGINS`, `INSTANCE_ID`, `LOG_FORMAT` (`text` or `json`), `METRICS_ENABLED`, and `LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING`, or `ERROR`). Environment variables override the local file.

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
- Web research (Forge M4.1) enforces bounded streaming response limits, SSRF prevention (blocking private, loopback, link-local, and cloud metadata IPs), DNS pinning to mitigate DNS rebinding, manual redirect validation, and structural prompt-injection delimiter escaping. These defenses contain risk and enforce boundaries; they do not claim an LLM is immune to semantic injection. See [Architecture](docs/ARCHITECTURE.md).

## Deployment and support

Production releases should use explicit HTTPS CORS origins, a secret-managed
`FORGE_API_KEY` of at least 32 characters, and a mounted persistent workspace
directory. `GET /api/v1/health` is suitable for liveness,
`GET /api/v1/ready` reports whether the workspace and API key are configured,
and `GET /api/v1/metrics` exposes Prometheus operational metrics across instances.

The repository includes backend and frontend container images. Build them from
the repository root:

```powershell
docker build -f backend/Dockerfile -t forge-backend .
docker build -f frontend/Dockerfile -t forge-frontend .
```

Use secrets and TLS through the deployment platform rather than Docker build
arguments or `VITE_*` variables. The API key is shared bearer tooling, not user
identity or authorization. Add a real identity and authorization system before
exposing workspace or execution capabilities to multiple users.

Report suspected vulnerabilities privately through the repository's security
policy. Operational support is provided on a best-effort basis in the issue
tracker; do not include provider keys or workspace contents in reports.

## License

MIT; see [LICENSE](LICENSE).
