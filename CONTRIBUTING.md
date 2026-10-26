# Contributing

Start with the README commands. Change `web/app.ts`, then run `npm ci && npm run build`; commit both TypeScript and generated JavaScript. Keep the Python runtime dependency-free.

For queue changes, add a test for a real failure mode: contention, restart, expiry, or invalid ownership. Use an injected clock. HTTP changes should include request-level tests. Explain changes to delivery guarantees or the trust boundary in `docs/`.

Use synthetic payloads and `.test` email addresses in fixtures. Do not commit local databases, secrets, or production traces. This repository has no external service credentials.
