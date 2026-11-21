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
- [x] Add read-only workspace discovery, bounded text reads/search, and workspace instructions loading.
- [x] Expose the read-only workspace operations through `forge files`, `forge read`, and `forge search`.

### Not implemented yet

- [ ] Agent execution loop and interactive chat endpoint.
- [ ] Gemini and OpenRouter inference adapters.
- [ ] Expose provider generation through an API or user-facing one-shot command.
- [ ] File write tools, permission prompts, and controlled terminal execution.
- [ ] Session persistence, resume, and change history.
- [ ] Frontend chat/session workflow and settings management.
- [ ] Frontend automated tests and end-to-end coverage.
- [ ] Production deployment, authentication, rate limiting, and operational observability.

The Ollama adapter can list installed models and generate non-streaming text for backend callers. `forge models` checks the local Ollama service. The current prompt CLI is still a placeholder, and provider generation is not exposed through the HTTP API. Gemini and OpenRouter adapters remain planned.

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
- [ ] Implement Gemini and OpenRouter adapters.

**Current result:** backend tests exercise Ollama model discovery and generation through the shared interface using a mock transport. Cloud adapters and user-facing generation endpoints remain follow-up work.

### 3. Build the guarded agent core

- [x] Add workspace context discovery and explicit workspace-root boundaries.
- [x] Implement bounded read/list/search tools before write/command tools.
- Require clear permission policy for file writes and subprocess execution.
- Add tool input validation, timeouts, cancellation, and auditable results.
- Connect the CLI and HTTP API to shared agent services rather than duplicating behavior.

**Current result:** read-only workspace inspection is available from the CLI and shared backend service, with traversal, symlink escape, secret-file, size, and scan-limit checks. Agent execution, file writes, and terminal commands are intentionally still unavailable.

**Done when:** a bounded task can run against a test workspace, with denied actions remaining non-mutating and all changes inspectable.

### 4. Add sessions and persistence

- Define SQLite schema and migration/versioning approach.
- Persist conversation turns, tool outcomes, and session metadata.
- Support creating, listing, resuming, and safely deleting sessions.

**Done when:** session state survives process restarts and API/CLI operations use the same domain behavior.

### 5. Connect the web workflow

- Add chat/session endpoints and streaming only after the backend execution contract is stable.
- Implement session selection, prompt submission, progress, cancellation, and error states.
- Keep provider credentials and execution policy on the server.
- Add frontend unit/component and end-to-end tests for success, failure, offline, and cancellation states.

**Done when:** a user can submit a task through the UI, follow execution, inspect changes, and see an honest failure state.

### 6. Prepare a production release

- Choose deployment topology and supported database/storage options.
- Add authentication/authorization before exposing workspace or execution endpoints.
- Configure production CORS, secret management, health/readiness probes, and structured logs.
- Add CI for Python tests/build and frontend lint/build.
- Document installation, upgrades, support policy, and security reporting.

**Done when:** a reproducible release pipeline and deployment instructions exist, with security controls enabled by default.

## Change checklist

For each implementation step:

1. Update this document and [Architecture](ARCHITECTURE.md) if component boundaries or API contracts change.
2. Keep API and service behavior covered by focused automated tests.
3. Keep secrets out of source, logs, browser bundles, and test output.
4. Run backend tests/build and frontend lint/build for affected applications.
5. Update [README](../README.md) and [Development](DEVELOPMENT.md) when setup or user-visible behavior changes.
