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

The provider adapters are `OllamaProvider`, `GeminiProvider`, and `OpenRouterProvider`:

- Ollama reads the server-only `OLLAMA_HOST` setting and uses `/api/tags` to list installed models and `/api/chat` for non-streaming and streaming generation.
- Gemini uses the server-only `GEMINI_API_KEY` with the Gemini REST API; OpenRouter uses the server-only `OPENROUTER_API_KEY` with its OpenAI-compatible REST API.
- All adapters implement `list_models()`, `generate()`, and `stream_generate()` yielding `GenerationChunk` objects.
- Requests have a 30-second timeout, generation is capped at 8,192 tokens, and response bodies are limited to 2 MiB.
- Network, timeout, HTTP, malformed response, and oversized response failures map to typed provider errors without returning upstream response bodies.
- Cancellation is not swallowed.
- `forge models` exercises model discovery for configured providers. `POST /api/v1/generate` exposes bounded generation behind a bearer API key; `POST /api/v1/agent/tasks` and `POST /api/v1/agent/tasks/stream` expose bounded agent tasks with session persistence.

Provider API keys must remain server-side and must not be included in logs or frontend responses.

## Read-only workspace context

`backend/src/forge/context/workspace.py` provides bounded workspace file discovery, UTF-8 reads, text search, optional `FORGE.md` instruction loading, and guarded file writes. Operations accept workspace-relative paths and patterns, reject traversal and absolute paths, resolve symlinks before access, and exclude common generated directories and credential-bearing files. Reads and scans have byte, result, and candidate limits. Binary and oversized files are skipped in searches and reported in the result metadata.

The CLI agent loop is opt-in through `forge -p "..." --agent`. It caps provider/tool cycles, accepts only structured read, search, write, and command tool calls, and feeds bounded tool results back into the next provider request. Read tools run directly; write tools reuse preview-first workspace writes; command tools reuse the allowlisted, shell-free command runner. Every write and command action requires a fresh default-no approval callback. The loop is not exposed through HTTP agent routes, so API callers remain read-only. Tool outcomes persisted to sessions contain only the tool name and success status, not file content or command output.

The CLI exposes these capabilities as `forge files`, `forge read`, `forge search`, and `forge write`. Writes are proposed as a unified diff and require an explicit confirmation callback; the operation rechecks the previous content hash after approval, writes through a temporary file followed by atomic replacement, and returns/logs hashes and byte counts without file contents. Write targets must not be symlinks and their parent directories must already exist.

`POST /api/v1/agent/tasks` reuses the same `Workspace` service for a stateless, read-only task. Its root comes only from server-side `AGENT_WORKSPACE_ROOT`; callers may request up to 10 workspace-relative context files, while the service also provides a bounded file list and optional `FORGE.md`. The route never invokes workspace writes or command execution.

## Session persistence

`backend/src/forge/sessions/store.py` owns a versioned SQLite store at `<workspace>/.forge/sessions.sqlite3`. It persists session metadata, ordered conversation turns, and non-text generation outcome metadata. SQLite foreign keys cascade session deletion to its turns and outcomes. The API and CLI both instantiate this service against an explicit workspace root; no session data is stored in browser state. A task request with a saved session ID restores prior turns, then appends the new turns, assistant reply, and generation metadata after successful completion.

`backend/src/forge/execution/commands.py` implements `forge run`. It allows only `git`, `npm`, `node`, `python`, `python3`, `pytest`, `ruff`, and `uv`; rejects executable paths and shell interpreters; invokes processes without a shell; requires a per-command approval callback; and limits arguments, execution time, and captured output. The CLI shows the resolved executable, full argument vector, workspace-relative cwd, and timeout before prompting with a default of no. The process receives only a small environment allowlist plus `PYTHONIOENCODING` and `UV_OFFLINE`; audit records omit arguments and process output. Timeouts and cancellation terminate the process group on POSIX and use Windows `taskkill /T /F` with parent-process fallback. Workspace cwd restrictions and explicit approval are not a security sandbox: approved programs still run with the current user's OS permissions. Command execution is currently CLI-only and is not exposed through the HTTP API.

## Current API contract

### `GET /api/v1/health`

Returns `{"status":"ok","service":"forge-api"}` when the API process responds.

### `GET /api/v1/ready`

Returns readiness only when `FORGE_API_KEY` is configured and
`AGENT_WORKSPACE_ROOT` resolves to an existing directory. Otherwise it returns
503 with the common error envelope.

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

### `POST /api/v1/generate`

Requires `Authorization: Bearer <FORGE_API_KEY>`. The configured key must be at least 32 characters, and production settings require it. The request uses the provider-neutral `GenerationRequest` schema (`model`, 1-64 messages, and `max_tokens` from 1 to 8,192); individual message content is capped at 64,000 characters. The route dispatches to the configured `DEFAULT_MODEL` provider (`ollama`, `gemini`, or `openrouter`). Success returns a `GenerationResult` with the model, assistant content, and completion flag. Authentication failures, unsupported providers, provider failures, and timeouts use the common error envelope with 401, 501, 502/503, or 504 status codes. The endpoint does not read or modify workspaces and does not execute commands.

### `POST /api/v1/agent/tasks`

Requires `Authorization: Bearer <FORGE_API_KEY>` and a server-configured `AGENT_WORKSPACE_ROOT`. The request accepts a model, 1-32 provider-neutral chat messages, an output limit, and up to 10 workspace-relative context paths. The service builds bounded, read-only context from the configured workspace and sends it to the selected provider. It returns the generation result, configured workspace root, context paths, and `read_only: true`. It does not accept arbitrary filesystem roots, write files, execute commands, persist sessions, or stream output.

When `session_id` is supplied, the route restores the saved conversation before generation and records the new turns, assistant reply, and generation metadata after success. It remains read-only with respect to project files.

### `POST /api/v1/agent/tasks/stream`

Requires `Authorization: Bearer <FORGE_API_KEY>` and a server-configured `AGENT_WORKSPACE_ROOT`. Accepts the same request body as `POST /api/v1/agent/tasks`. Returns a `text/event-stream` Server-Sent Events stream.

Stream events follow this protocol:
- Intermediate token events: `data: {"content": "text chunk", "done": false, "model": "..."}\n\n`
- Terminal completion event: `data: {"content": "", "done": true, "model": "...", "session_id": "..."}\n\n`
- Error events: `event: error\ndata: {"error": {"code": "...", "message": "..."}}\n\n`

When `session_id` is provided, user turns, completed assistant text, and generation outcome are committed to SQLite session storage upon stream completion.

### Deployment topology

Forge is structured for single-tenant or isolated team environments:
- **Standalone local container**: Docker Compose or single container running both backend and frontend, with the target workspace mounted into `/workspace` and `AGENT_WORKSPACE_ROOT=/workspace`.
- **Single-tenant container service**: Backend container running behind a TLS reverse proxy (Caddy/Nginx) enforcing HTTPS, with volume-mounted storage for `.forge/sessions.sqlite3`.
- **Secret management**: Secrets (`FORGE_API_KEY`, provider API keys) must be injected via runtime environment variables or container secret managers (e.g., Docker secrets, Kubernetes secrets), never committed in `.env.local` or built into container images.
- **Rate limiting at edge**: For multi-instance deployments, process-local rate limiting should be complemented by a shared edge rate limiter (e.g. Cloudflare, Nginx `limit_req`, or Redis-backed API gateway).

### Session endpoints

`POST /api/v1/sessions`, `GET /api/v1/sessions`, `GET /api/v1/sessions/{session_id}`, and `DELETE /api/v1/sessions/{session_id}` require the generation bearer key and configured agent workspace root. They create, list, retrieve, and delete sessions in the workspace-local SQLite store. Session IDs are opaque UUID values; deletion targets one exact ID and cascades only its associated turns and outcomes.

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
- `FORGE_API_KEY` is a server-only bearer key for model generation; production requires a key of at least 32 characters.
- `AGENT_WORKSPACE_ROOT` is a server-only, explicit root for the read-only agent task endpoint; callers cannot override it.
- Session data is retained at `AGENT_WORKSPACE_ROOT/.forge/sessions.sqlite3`, which workspace tools exclude from readable context.
- `ENVIRONMENT` selects `development`, `test`, or `production`; `LOG_LEVEL` selects the backend log level.
- `RATE_LIMIT_REQUESTS` and `RATE_LIMIT_WINDOW_SECONDS` configure a process-local sliding-window limit for authenticated generation, task, and session routes. It uses the direct client address and intentionally ignores forwarded-address headers; production deployments with multiple instances require an equivalent shared edge limit.
- The API allows GET and POST plus the Authorization header and does not enable credentialed CORS.
- The UI displays backend connection failures; it does not treat an unavailable API as a successful response.
- Workspace writes and CLI commands require explicit approval; neither capability is exposed through the HTTP API. The agent task route is read-only, and generation is separately protected by the API key.

## Extension conventions

- Keep route definitions in `backend/src/forge/api/routes/`; register them in `api/main.py`.
- Keep API request and response schemas explicit and version routes under `/api/v1`.
- Put domain behavior in backend services rather than route functions or React components.
- Add backend tests for new API/service behavior and frontend tests for user-facing state transitions as those capabilities are introduced.
- Update this document when process boundaries or API contracts change.
