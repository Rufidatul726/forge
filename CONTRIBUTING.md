# Contributing

Contributions and issue reports are welcome. Forge is early-stage; check [Implementation status](docs/IMPLEMENTATION.md) before starting larger changes.

## Before opening a change

1. Keep backend and frontend responsibilities in their respective folders.
2. Discuss changes that alter API contracts, execution permissions, persistence, or deployment boundaries.
3. Add focused tests for behavior being introduced or changed.
4. Update the relevant documentation and implementation checklist.
5. Never commit `.env.local` files, API keys, or other credentials.

## Validation

Run the backend checks from the repository root:

```powershell
uv run --project backend --extra dev pytest backend\tests
uv build --project backend
```

Run the frontend checks from `frontend/`:

```powershell
npm run lint
npm run build
```

See [Development](docs/DEVELOPMENT.md) for local setup and commands.
