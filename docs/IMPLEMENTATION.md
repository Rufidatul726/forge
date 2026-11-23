# Implementation status and steps

This document is the implementation checklist and handoff point for the project. It distinguishes working foundations from planned work; README feature descriptions must not imply that planned behavior is already available.

## Current status

### Completed foundation

- [x] Separate Python backend and JavaScript frontend directories.
- [x] Keep the Typer CLI as a backend package entry point.
- [x] Add a FastAPI application with versioned health and provider-status routes.
- [x] Add a React + TypeScript + Vite dashboard that reads API health and provider status.
- [x] Add development CORS configuration and separate backend/frontend environment examples.
- [x] Ignore local environment files so credentials are not committed.
- [x] Add backend CLI/API tests and document setup, boundaries, and commands.
- [x] Define documented response models and consistent validation, HTTP, and server error responses.
- [x] Add request IDs, request-completion logging, and validated runtime/CORS settings.
- [x] Define a provider protocol and provider-neutral generation models.
- [x] Implement the Ollama adapter with bounded requests, timeouts, cancellation, and safe error mapping.
- [x] Implement Gemini and OpenRouter adapters through the shared provider interface.
- [x] Add bounded workspace discovery, text reads/search, and instruction loading.
- [x] Add preview-first file writes with explicit approval, stale-content checks, atomic replacement, and hash-only audit logs.
- [x] Expose workspace operations through `forge files`, `forge read`, `forge search`, and `forge write`.
- [x] Add controlled terminal execution with an executable allowlist, exact-command approval, bounded output, timeout/cancellation cleanup, and redacted audit logs.
- [x] Expose bounded Ollama generation through an API-key-protected API endpoint.
- [x] Add a stateless, authenticated, read-only workspace task endpoint.
- [x] Add real-time SSE streaming for agent tasks (`POST /api/v1/agent/tasks/stream`) with provider-level streaming across Ollama, Gemini, and OpenRouter.
- [x] Expand frontend automated unit and component test coverage for sessions, settings, streaming, and cancellation.

### Not implemented yet

- [ ] Autonomous agent execution loop with user-approved tool mutations.
- [x] Connect the CLI prompt runner to the read-only agent service and persistent sessions.
- [x] Add SQLite session persistence, listing, resume, and safe deletion.
- [x] Persist project change history after approved workspace writes are connected to sessions.
- [x] Frontend chat/session workflow, real-time streaming, and settings management.
- [x] Frontend automated tests and end-to-end component coverage.
- [ ] User identity authentication/authorization (OAuth/OIDC) and multi-instance distributed observability.

Ollama, Gemini, and OpenRouter adapters support model listing, non-streaming generation, and streaming token generation. `POST /api/v1/generate` exposes bounded generation behind `FORGE_API_KEY`; `POST /api/v1/agent/tasks` and `POST /api/v1/agent/tasks/stream` provide read-only repository task workflows with session persistence. `forge -p` runs the guarded read-only service locally and persists turns. Autonomous tool loops and mutations remain follow-up work.

## Recommended implementation sequence

### 1. Stabilize contracts and runtime

- [x] Define typed API response models and consistent error responses.
- [x] Add health and provider-status tests that isolate environment settings.
- [x] Establish request logging, request IDs, and configuration validation.
- [x] Keep secrets server-side and require explicit production CORS configuration.

**Done:** API success and error schemas are documented in OpenAPI and tested; invalid production CORS configuration fails during settings validation.

### 2. Add model providers

- [x] Introduce a provider protocol and provider-neutral generation models.
- [x] Implement Ollama model discovery and non-streaming generation.
- [x] Add timeouts, cancellation, bounded response handling, and safe error mapping.
- [x] Keep provider credentials out of logs and browser payloads.
- [x] Expose generation through `POST /api/v1/generate` with bearer-key authentication.
- [x] Implement Gemini and OpenRouter adapters.

**Current result:** backend tests exercise Ollama, Gemini, and OpenRouter model discovery and generation through the shared interface using mock transports. The generation endpoint accepts bounded provider-neutral requests, requires `FORGE_API_KEY`, dispatches through the configured default provider, and maps upstream failures to the shared sanitized error contract. A user-facing chat/agent workflow remains follow-up work.

### 3. Build the guarded agent core

- [x] Add workspace context discovery and explicit workspace-root boundaries.
- [x] Implement bounded read/list/search tools.
- [x] Add diff-previewed writes requiring explicit approval, with stale-content detection and auditable results.
- [x] Connect CLI workspace operations to the shared workspace service.
- [x] Add subprocess execution controls, timeouts, cancellation, and audit results.
- [x] Connect authenticated API workflows to shared read-only agent services.

**Current result:** workspace reads/searches are bounded; file writes require an explicit approval callback and are previewed in the CLI. Sensitive paths, symlink write targets, traversal, oversized content, and stale proposals are rejected. `forge run` accepts a narrow executable allowlist and requires default-no confirmation of the resolved executable and exact arguments on every run. Commands are shell-free, workspace-cwd bounded, timeout-limited, output-capped, and inherit only an environment allowlist. The API supports authenticated, read-only tasks against an explicit server-configured root; writes, commands, sessions, streaming, and autonomous tool loops remain unavailable. Command approval is not an OS sandbox.

**Done when:** a bounded task can run against a test workspace, with denied actions remaining non-mutating and all changes inspectable.

### 4. Add sessions and persistence

- [x] Define a versioned SQLite schema under the workspace `.forge` directory.
- [x] Persist conversation turns, generation outcomes, and session metadata.
- [x] Support creating, listing, resuming, and safely deleting sessions through API and CLI.

**Current result:** session state survives process restarts in `<workspace>/.forge/sessions.sqlite3`. API and CLI use the same store; task resume restores saved turns and records successful assistant results. Approved CLI writes can use `forge write --session <id>` to append hash-only `workspace_write` outcomes. Project file mutations remain approval-gated and outside the HTTP API.

### 5. Connect the web workflow

- [x] Add chat/session endpoints after the backend execution contract.
- [x] Implement session selection, prompt submission, progress, cancellation, and error states.
- [x] Keep provider credentials and execution policy on the server.
- [x] Add frontend unit/component tests for success, failure, offline, cancellation, and streaming states.

**Current result:** the React dashboard includes session selection, creation, and deletion, persistent chat turns, model/context controls, real-time SSE streaming responses with typing indicators, cancellation, loading, offline, and API error states. The backend exposes `POST /api/v1/agent/tasks/stream` alongside the non-streaming task endpoint, with streaming support across Ollama, Gemini, and OpenRouter adapters.

**Done when:** a user can submit a task through the UI, follow execution in real time or non-streaming mode, inspect changes, and see an honest failure state.

### 6. Prepare a production release

- [x] Choose deployment topology (standalone local container or single-tenant container service with volume-mounted workspace).
- Add user authentication/authorization before exposing workspace or execution endpoints; the generation API key is not user identity or a replacement for that work.
- [x] Configure production CORS and add liveness/readiness probes.
- [x] Add process-local rate limiting and production secret-management guidance.
- [x] Add CI for Python tests/build and frontend tests/lint/build.
- [x] Document container builds and release security boundaries.

**Current result:** authenticated generation, task, streaming task, and session routes have a configurable process-local sliding-window limit. Production guidance recommends single-tenant containerized deployment with volume-mounted workspace root, deployment-managed secret injection, and a shared edge limiter for multi-instance deployments; multi-tenant user authentication and distributed operational observability remain follow-up work.

**Done when:** a reproducible release pipeline and deployment instructions exist, with security controls enabled by default.

## Change checklist

For each implementation step:

1. Update this document and [Architecture](ARCHITECTURE.md) if component boundaries or API contracts change.
2. Keep API and service behavior covered by focused automated tests.
3. Keep secrets out of source, logs, browser bundles, and test output.
4. Run backend tests/build and frontend lint/build for affected applications.
5. Update [README](../README.md) and [Development](DEVELOPMENT.md) when setup or user-visible behavior changes.
