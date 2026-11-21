# Security policy

Report suspected vulnerabilities privately to the repository maintainers.
Do not open a public issue containing credentials, private workspace data,
provider responses, or proof-of-concept exploit details.

For an emergency issue, include the affected version, deployment environment,
minimal reproduction steps, and available logs with secrets removed.
Maintainers will coordinate triage and disclosure.

## Supported security boundaries

- Provider credentials and `FORGE_API_KEY` belong in backend environment or
  secret-manager configuration.
- `VITE_*` values are browser-visible and must never contain secrets.
- Production CORS requires explicit HTTPS origins.
- The API bearer key is shared tooling authentication, not user identity.
- Workspace writes and command execution remain approval-gated and are not a
  remote multi-user authorization boundary.
