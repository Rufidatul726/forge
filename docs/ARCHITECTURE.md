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

`backend/src/forge/providers/protocol.py` defines the async `ModelProvider` interface. Provider-neutral chat messages, generation options, and structured tool calling models live in `providers/models.py`; adapters map those models to provider-specific protocols and raise typed errors with safe public messages.

The provider-neutral models for structured tool calling are:
- `ToolDefinition`: Describes a tool with its name, description, and strict JSON Schema parameter specifications.
- `ToolCall`: Represents a structured tool invocation requested by the model with an identifier, tool name, and parsed arguments.
- `ToolResult`: Represents the result of a tool execution sent back to the model, including execution status and text content.
- `AssistantMessage`: An assistant message containing optional text content and/or structured `ToolCall` instances.
- `ProviderResponse`: Standardized provider response holding model name, assistant message, structured tool calls, completion status, and text content (with `GenerationResult` subclass for backward compatibility).

The provider adapters are `OllamaProvider`, `GeminiProvider`, and `OpenRouterProvider`:

- Ollama reads the server-only `OLLAMA_HOST` setting and uses `/api/tags` to list installed models and `/api/chat` for generation and native structured tool calling via the `tools` array.
- Gemini uses the server-only `GEMINI_API_KEY` with the Gemini REST API (`generateContent` with `functionDeclarations` and `functionCall`/`functionResponse` mapping).
- OpenRouter uses the server-only `OPENROUTER_API_KEY` with its OpenAI-compatible REST API (`chat/completions` with native tool definitions and `tool_calls`).
- All adapters implement `list_models()`, `generate()`, and `stream_generate()` yielding `GenerationChunk` objects.
- Requests have a 30-second timeout, generation is capped at 8,192 tokens, and response bodies are limited to 2 MiB.
- Network, timeout, HTTP, malformed response, and oversized response failures map to typed provider errors without returning upstream response bodies.
- Cancellation is not swallowed.
- `forge models` exercises model discovery for configured providers. `POST /api/v1/generate` exposes bounded generation behind a bearer API key; `POST /api/v1/agent/tasks` and `POST /api/v1/agent/tasks/stream` expose bounded agent tasks with session persistence.

Provider API keys must remain server-side and must not be included in logs or frontend responses.

## Read-only workspace context and tool architecture

`backend/src/forge/context/workspace.py` provides bounded workspace file discovery, UTF-8 reads, text search, optional `FORGE.md` instruction loading, and guarded file writes. Operations accept workspace-relative paths and patterns, reject traversal and absolute paths, resolve symlinks before access, and exclude common generated directories and credential-bearing files. Reads and scans have byte, result, and candidate limits. Binary and oversized files are skipped in searches and reported in the result metadata.

### Tool registry, strict schemas, and dispatcher

Tools are managed through `ToolRegistry` (`backend/src/forge/agent/registry.py`) and executed through `ToolDispatcher` (`backend/src/forge/agent/dispatcher.py`):
- `read_file`: Reads UTF-8 contents of a relative workspace file. Strict schema `ReadFileArgs` forbids extra fields.
- `search_text`: Performs bounded text searches across readable files. Strict schema `SearchTextArgs` forbids extra fields.
- `search_symbols`: Performs symbol searches (classes, functions, methods, interfaces, types) across code files. Strict schema `SearchSymbolsArgs` forbids extra fields.
- `list_files`: Lists workspace files matching glob patterns. Strict schema `ListFilesArgs` forbids extra fields.
- `write_file`: Preview-first workspace file modification requiring explicit confirmation. Integrates with `CheckpointManager` to snapshot pre-change states. Strict schema `WriteFileArgs` forbids extra fields.
- `run_command`: Execution of allowlisted development executables without a shell. Strict schema `RunCommandArgs` forbids extra fields.

Every tool argument payload is validated against strict Pydantic schemas with `model_config = ConfigDict(extra="forbid")`. Unknown tools, malformed arguments, and unexpected fields are rejected immediately with structured error results and do not break the agent execution loop.

### Execution policies and configuration

Execution policies are governed by `ExecutionPolicyConfig` (`backend/src/forge/agent/policy.py`):
- `plan`: Read-only. File modifications and command executions are strictly prohibited and rejected before prompting for approval.
- `normal`: Read operations are automatic; file writes and commands require interactive user approval.
- `autonomous`: Only explicitly permitted actions are executed without interactive confirmation. Disabled by default.

Policy configuration is resolved hierarchically through `load_execution_policy_config`:
1. Global configuration: `~/.forge/config.json`.
2. Project configuration: `<workspace>/.forge/config.json`.
3. CLI overrides: `--policy <plan|normal|autonomous>`, `--plan` (forces `plan` mode), and `--allow-tool <tool>` (explicit per-tool autonomous permissions).

In `plan` mode, file modifications and commands are unconditionally blocked before approval callbacks, guaranteeing strict read-only safety regardless of configuration.

### Checkpoints and rollback architecture

`CheckpointManager` (`backend/src/forge/context/checkpoint.py`) tracks file states before and after task executions:
- **Task ID format validation**: Task IDs are strictly validated (`^[A-Za-z0-9_-]+$`, max 128 chars) to prevent directory traversal or checkpoint filename manipulation.
- **Path and root security**: Restored paths are validated against the canonical workspace root; absolute paths, directory traversal sequences, symlinks, and files excluded by workspace security rules (`.env`, secrets, `.git`, ignored directories) are strictly rejected.
- **Payload versioning & canonical checksum**: Checkpoint files on disk are versioned (`version: 1`) and contain a deterministic SHA-256 canonical payload checksum over the sorted JSON fields. Rollback validates this checksum to detect accidental file corruption or payload truncation before modifying workspace files. Unkeyed checksums detect accidental corruption; authenticated protection against local malicious tampering requires keeping HMAC keys outside the workspace.
- **Pre-change state recording**: Before any file is modified or created in a task, its original pre-change content and SHA-256 hash are recorded. Subsequent edits within the same task preserve the original baseline. Unchanged files and rejected writes never record checkpoint snapshots.
- **Post-change verification**: Post-change hashes are recorded from the actual resulting file on disk upon successful write.
- **Conflict detection and safe rollback**: During rollback (`forge rollback` or `mgr.rollback(task_id)`), current file content is compared against the recorded post-change hash. If external changes occurred after the task, or if an existed file was deleted, rollback flags a conflict and skips the file to ensure unrelated user edits are never overwritten. Tampered snapshot data (such as checksum mismatches or corrupted payloads) is flagged as a conflict. Unmodified task changes are safely reverted (deleting files created in the task and restoring modified files atomically).

### Repository intelligence and context engineering (Forge M3)

The repository intelligence system (`backend/src/forge/intelligence/`) provides bounded, incremental, persistent repository understanding and task-specific context retrieval:

1. **Repository scanner & inventory (`scanner.py`)**:
   - Walks the workspace directory hierarchy honoring `.gitignore`, workspace exclusions, and sensitive files (`.env`, credentials, secrets, `.git`, `.forge`, `node_modules`).
   - Rejects directory traversal and symlink escapes without following symlinks.
   - Computes streaming content SHA-256 hashes and file metadata bounded by `max_files` (default: 5,000) and `max_file_bytes` (default: 1 MiB).
   - Detects project manifests (`pyproject.toml`, `requirements.txt`, `package.json`, `tsconfig.json`) and parses frameworks (FastAPI, React, Vite, pytest, etc.).
   - Extracts Git metadata (branch, commit SHA, dirty status, modified files) when available without requiring Git.
   - Generates deterministic repository maps with hierarchical directory trees and project summaries.

2. **Language symbol analysis (`parsers/`)**:
   - AST-based parsing for Python using standard library `ast`.
   - Structural and token parsing for JavaScript, TypeScript, TSX, and JSX.
   - Extracts functions, classes, methods, constants, line numbers, docstrings, and signatures.
   - Tracks parent-child symbol relationships (methods belonging to classes, nested functions).
   - Extracts imports and resolves references: resolves calls and instantiations to local declarations or imported module symbols, clearly distinguishing resolved references from unresolved/textual matches.
   - Resilient against syntax errors and malformed files without aborting indexing runs.

3. **Dependency and architecture intelligence (`architecture.py`)**:
   - Detects backend and frontend project boundaries from manifests and directory conventions.
   - Detects statically declared API routes (FastAPI `@app.get`, `@router.post`, Express `app.get`).
   - Detects database models (SQLAlchemy, Django) and migration directories (`alembic/`, `migrations/`).
   - Identifies test suites (pytest, vitest/jest) and config files.
   - Maps module coupling via static imports with explicit confidence levels.

4. **Persistent SQLite index (`store.py`, `indexer.py`)**:
   - Versioned SQLite database at `<workspace>/.forge/index.sqlite3` with schema migration tracking.
   - Tables: `repositories`, `files`, `symbols`, `imports`, `references_index`, `dependencies`, `index_runs`.
   - Incremental indexing: compares file content hashes and modification times to re-index only added or modified files, cleans up deleted files, and tracks renames.
   - Transactional safety: runs in SQLite WAL mode. Indexing runs are atomic; unhandled failures roll back changes and mark runs as failed. Interrupted runs are recovered safely on restart. Corrupted database files trigger automated fresh re-initialization.

5. **Explainable context retrieval engine (`retrieval.py`)**:
   - Task-oriented keyword and symbol extraction from user prompts.
   - Multi-factor explainable relevance ranking: exact/substring symbol matches (+30-50), keyword path matches (+15), Git modifications boost (+40), associated test file matches (+25), and cross-file symbol references (+10).
   - Deduplicates context items and attaches explicit relevance scores and reasons.

6. **Token-budget-aware context builder (`context.py`)**:
   - Enforces a configurable token budget ceiling with conservative estimation fallback (`chars // 4`).
   - Fixed priority order: Project instructions (up to 25%), Repository map summary (up to 15%), Scored source files, Associated test files.
   - Hard budget enforcement: strictly truncates or excludes lower-priority items when approaching the token limit without silent overflow.
   - Generates a context manifest recording included items (path, tokens, score, reasons) and excluded items.

7. **Agent integration & CLI commands**:
   - Agent loop automatically retrieves task-relevant context before making decisions.
   - CLI commands: `forge index`, `forge index --status`, `forge index --force`, `forge map`, `forge symbols <query>`, `forge references <symbol>`, `forge explain <path>`, and `forge context <task>`.


`Workspace.discover_context` discovers root and nested `FORGE.md` and `AGENTS.md` instruction files throughout the repository hierarchy, sorting them so root guidelines take precedence. Context selection allocates explicit token budgets across instructions (~30%), repository file trees (~25%), and requested context files (~45%), fairly partitioning excerpts and strictly enforcing the overall token ceiling. `.gitignore` patterns and sensitive credential/key patterns are respected across all searches, listings, and reads.

The CLI agent loop is opt-in through `forge -p "..." --agent`. It caps provider/tool cycles at 8 steps, issues native structured tool definitions to the provider, dispatches structured tool calls, and returns tool results to the provider in subsequent turns.
When `--plan --agent` is invoked, the `plan` execution policy is enforced, guaranteeing that no file writes or command executions can occur.
The autonomous tool loop is not exposed through HTTP agent routes, so API callers remain strictly read-only. Tool outcomes persisted to sessions contain only the tool name and success status, not file content or command output.

The CLI exposes these capabilities as `forge files`, `forge read`, `forge search`, and `forge write`. Writes are proposed as a unified diff and require an explicit confirmation callback; the operation rechecks the previous content hash after approval, writes through a temporary file followed by atomic replacement, and returns/logs hashes and byte counts without file contents. Write targets must not be symlinks and their parent directories must already exist.

`POST /api/v1/agent/tasks` reuses the same `Workspace` service for a stateless, read-only task. Its root comes only from server-side `AGENT_WORKSPACE_ROOT`; callers may request up to 10 workspace-relative context files, while the service also provides a bounded file list and optional `FORGE.md`. The route never invokes workspace writes or command execution.

## Session persistence

`backend/src/forge/sessions/store.py` owns a versioned SQLite store at `<workspace>/.forge/sessions.sqlite3`. It persists session metadata, ordered conversation turns, and non-text generation outcome metadata. SQLite foreign keys cascade session deletion to its turns and outcomes. The API and CLI both instantiate this service against an explicit workspace root; no session data is stored in browser state. A task request with a saved session ID restores prior turns, then appends the new turns, assistant reply, and generation metadata after successful completion.

`backend/src/forge/execution/commands.py` implements `forge run`. It allows only `git`, `npm`, `node`, `python`, `python3`, `pytest`, `ruff`, and `uv`; rejects executable paths and shell interpreters; invokes processes without a shell; requires a per-command approval callback; and limits arguments, execution time, and captured output. The CLI shows the resolved executable, full argument vector, workspace-relative cwd, and timeout before prompting with a default of no. The process receives only a small environment allowlist plus `PYTHONIOENCODING` and `UV_OFFLINE`; audit records omit arguments and process output. Timeouts and cancellation terminate the process group on POSIX and use Windows `taskkill /T /F` with parent-process fallback. Workspace cwd restrictions and explicit approval are not a security sandbox: approved programs still run with the current user's OS permissions. Command execution is currently CLI-only and is not exposed through the HTTP API.

## Current API contract

### `GET /api/v1/health`

Returns `{"status":"ok","service":"forge-api"}` when the API process responds.

### `GET /api/v1/ready`

Returns readiness only when authentication is configured for the active `AUTH_MODE`
(`FORGE_API_KEY` for `api_key` mode, or `OIDC_ISSUER` and `OIDC_AUDIENCE` for `oidc` mode)
and `AGENT_WORKSPACE_ROOT` resolves to an existing directory. Otherwise it returns
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

### Metrics endpoint

`GET /api/v1/metrics` exposes Prometheus-compatible text metrics (`text/plain; version=0.0.4; charset=utf-8`) for scraping across multi-instance clusters. It reports `forge_instance_info` (with instance ID, environment, and version), `forge_http_requests_total` partitioned by method, path, status code, and instance ID, `forge_http_request_duration_seconds` latency histograms and counts, `forge_http_requests_in_flight` active request gauges, and `forge_rate_limited_requests_total` counters.

## Error and request tracing contract

All responses include a server-propagated or server-generated `X-Request-ID`. In multi-instance distributed environments, the server also returns `X-Instance-ID` identifying the serving replica, preserves and echoes caller-provided `X-Correlation-ID` headers, and participates in W3C Trace Context (`traceparent` header propagation with per-hop child span IDs). The request and trace identifiers are present in request-completion logs and in the JSON body for errors. Validation errors include field locations, error messages, and types, but never echo submitted values. Unexpected errors return a generic message; logs record the exception type without exception text. Structured JSON logging (`LOG_FORMAT=json`) formats all log records as single-line JSON with timestamps, logger names, instance IDs, trace IDs, and performance metrics for centralized log aggregation.

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

## Web research and evidence-grounded coding (Forge M4)

The research subsystem (`backend/src/forge/research/`) provides provider-neutral web research, documentation retrieval, deterministic ranking, content caching, and citation-backed evidence grounding without introducing external agent frameworks or mandatory paid APIs:

### 1. Bounded HTTP retrieval and security model (`fetcher.py`)

`SafeDocumentFetcher` enforces strict boundaries against Server-Side Request Forgery (SSRF), DNS rebinding, and denial-of-service:
- **Protocol restrictions**: Allows HTTP and HTTPS only. Rejects `file:`, `gopher:`, `ftp:`, `data:`, and `javascript:` schemes. Embedded credentials in URLs are rejected.
- **SSRF defense & IP filtering**: Rejects `localhost`, local naming suffixes (`.local`, `.internal`, `.lan`, `.localhost`), and all private, loopback, link-local, multicast, and reserved IP ranges for both IPv4 and IPv6 (including IPv4-mapped IPv6 addresses, NAT64 prefixes, and documentation ranges).
- **DNS rebinding prevention**: Uses a custom `SafeNetworkBackend` injected into `httpcore`. Hostnames are resolved to IP addresses and pre-validated against disallowed subnets; the TCP socket connects directly to the pre-validated IP address, eliminating the race window between name resolution and socket connection. Full TLS SNI and server hostname validation are preserved.
- **Redirect security**: Disables automatic redirect following in `httpx`. The fetcher loops through redirects manually (up to a configurable maximum of 5), re-validating the target URL scheme, hostname, and resolved IP on every redirect hop. Sensitive headers are stripped when redirects transition across domains.
- **Resource bounding**: Enforces connection (5s) and read (10s) timeouts, inspects `Content-Length` headers, streams response chunks, and aborts immediately if response payload exceeds the maximum response limit (default 2 MiB).
- **Content type enforcement**: Permits only documentation and text MIME types (`text/html`, `application/xhtml+xml`, `text/plain`, `text/markdown`, `application/json`). Arbitrary binary files (executables, archives, media) are rejected.
- **Error sanitization**: User-facing exceptions never leak internal network topology, private IPs, or internal subnets.

### 2. Content extraction and normalization (`extraction.py`)

Extracts clean technical documentation while stripping page boilerplate:
- **Boilerplate exclusion**: Removes navigation headers (`<nav>`, `.navbar`, `.menu`), footers (`<footer>`), sidebars, cookie consent banners (`.cookie`, `.consent`, `.banner`), advertisements (`<aside>`, `.ad`), scripts, styles, SVG, and forms.
- **Structure preservation**: Preserves document titles, heading hierarchy (`h1`–`h6`), code blocks (`<pre><code>` with exact whitespace and indentation preserved), markdown tables, lists, and documentation links.
- **Multi-format support**: HTML parsing (`HTMLDocumentationParser`), Markdown extraction, structured JSON documentation extraction, and normalized plain text.
- **Content hashing**: Computes SHA-256 hashes of extracted text for content-based deduplication and cache validation.

### 3. Search provider abstraction (`providers/`)

Provider-neutral web search interface (`SearchProvider` protocol):
- **Brave Search adapter (`brave.py`)**: Official stable REST API supporting query submission, result limits, freshness/recency filters, domain restrictions via `site:<domain>`, and error normalization (rate limits, timeouts, authentication errors).
- **Tavily adapter (`tavily.py`)**: Developer REST API adapter supporting query limits and domain filtering.
- **SearXNG adapter (`searxng.py`)**: Self-hosted or public SearXNG instance integration via `SEARXNG_URL`.
- **Mock adapter (`mock.py`)**: Deterministic search adapter for automated test suites without live network dependencies.
- **Graceful degradation (`factory.py`)**: When no search provider is configured, `UnconfiguredSearchProvider` returns an actionable message explaining how to configure credentials, while direct URL fetching (`fetch_url`) remains fully functional.

### 4. Persistence and document cache (`storage.py`, `cache.py`)

- **Persistent cache**: Stores retrieved documentation in `.forge/research.sqlite3` with configurable TTL expiration (default 7 days), maximum document count limits (default 500), and maximum total cache size limits (default 50 MB) using deterministic FIFO/oldest-first eviction.
- **Transactional report storage**: Versioned schema migration (`schema_migrations`) managing atomic persistence of research queries (`research_queries`), sources (`research_sources`), join links (`research_query_sources`), evidence excerpts (`evidence_excerpts`), citations (`citations`), and research reports (`research_reports`).
- **Resilience**: Operates in WAL mode with busy timeouts and automatic corruption recovery.

### 5. Ranking, deduplication, and evidence grounding (`ranking.py`, `citations.py`)

- **Deterministic ranking**: Explainable multi-signal scoring: title keyword match (30%), heading keyword match (25%), content keyword density (20%), domain preference (25%), official documentation boost (15%), version matching (20% boost / -10% mismatch penalty), and reciprocal search rank.
- **Deduplication**: Prunes duplicate sources using canonical URLs, normalized URL queries (stripping tracking parameters like `utm_*`), and SHA-256 content hashes.
- **Verifiable citations**: Associates claims with stable identifiers (`src_1`, `src_2` / `[S1]`, `[S2]`). Citations require exact excerpt substrings found within retrieved sources and are validated before reports are generated.
- **Structured findings**: Distinguishes verified facts, multi-source inferences, recommendations, unverified assumptions, and unresolved questions. Detects conflicting versions or documentation discrepancies across retrieved sources.
- **Prompt-injection defense**: Wraps all external research evidence inside `<untrusted_evidence>` fences, neutralizes instruction override phrases (`[FILTERED_INSTRUCTION_OVERRIDE]`), and marks all research tools read-only.

### 6. Agent integration and CLI

- **Tool registry integration**: `web_search`, `fetch_url`, `search_documentation`, and `inspect_research` are registered with strict Pydantic argument schemas and marked `read_only=True`. They operate seamlessly across `plan`, `normal`, and `autonomous` execution policies without requiring write/command approvals.
- **CLI interface**: `forge research <query>`, `forge research fetch <url>`, `forge research show <id>`, `forge research sources <id>`, and `forge research cache (--status | --clear)`.

## Configuration and trust boundaries

- Backend settings are loaded from `backend/.env.local` and normal process environment variables. Environment variables take precedence.
- Frontend settings are loaded by Vite from `frontend/.env.local`.
- `VITE_*` values are included in browser code and are public. Provider secrets belong only in the backend.
- `OLLAMA_HOST` is validated as an HTTP(S) origin; embedded credentials, paths, and query parameters are rejected.
- `CORS_ORIGINS` is a comma-separated allowlist. Production requires explicit HTTPS origins and rejects wildcard and localhost origins.
- `FORGE_API_KEY` is a server-only bearer key for model generation; production requires a key of at least 32 characters when `AUTH_MODE=api_key`.
- `AUTH_MODE` selects authentication: `api_key` (default), `oidc`, or `api_key_or_oidc`.
- When `AUTH_MODE` is `oidc` or `api_key_or_oidc`, `OIDC_ISSUER` and `OIDC_AUDIENCE` are required HTTP(S) URLs (enforced HTTPS in production). Optional `OIDC_JWKS_URL` overrides well-known discovery. `OIDC_REQUIRED_SCOPE` and `OIDC_REQUIRED_ROLE` enforce token scope and role claims (returning 403 Forbidden when absent).
- `AGENT_WORKSPACE_ROOT` is a server-only, explicit root for the read-only agent task endpoint; callers cannot override it.
- Session data is retained at `AGENT_WORKSPACE_ROOT/.forge/sessions.sqlite3`, which workspace tools exclude from readable context.
- `ENVIRONMENT` selects `development`, `test`, or `production`; `LOG_LEVEL` selects the backend log level.
- `INSTANCE_ID` configures the unique node identifier (1-64 alphanumeric, hyphens, underscores, dots) for distributed clusters, auto-generated if omitted.
- `LOG_FORMAT` selects `text` (default) or `json` for machine-parseable structured logging.
- `METRICS_ENABLED` enables or disables Prometheus metrics scraping at `/api/v1/metrics`.
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
