# HTTP contract

Base URL: `http://127.0.0.1:8080`. Send `Content-Type: application/json` for POST. The queue defaults to `default`. Queue names use 1–64 ASCII letters, digits, `_` or `-`. All times in responses are Unix seconds.

| Method / path | JSON body / query | Success |
| --- | --- | --- |
| `GET /health` | none | `200 {"status":"ok"}` |
| `GET /api/jobs?queue=default` | query queue | `200 {counts,jobs}`; latest 100, tokens excluded |
| `POST /api/jobs` | `payload`, optional `queue`, `idempotency_key`, `delay`, `max_attempts` | `201 {job,created:true}`, or `200 {job,created:false}` on identical key replay |
| `POST /api/claim` | optional `queue`, `lease_seconds` | `200 {job}`; job is null or includes `lease_token` |
| `POST /api/jobs/{id}/ack` | `lease_token` | `200 {"ok":true}` |
| `POST /api/jobs/{id}/fail` | `lease_token`, optional `error` | `200 {"ok":true}` |
| `POST /api/jobs/{id}/retry` | `{}` | `200 {"ok":true}`; dead jobs only |

`payload` can be any finite JSON value and is limited to 64 KiB after canonical encoding. `delay` is an integer 0–86400 seconds, default 0. `max_attempts` is 1–100, default 3. `lease_seconds` is 1–3600, default 30. Booleans do not count as integers. Keys contain 1–128 characters. Error strings contain at most 1000 characters. Bodies are capped at 70000 bytes; transfer-encoded bodies are not supported.

Explicit failure schedules retry after `min(300, 2^attempts)` seconds, unless exhausted. Lease expiry makes the job eligible on the next claim, with no additional backoff. Manual replay clears error and resets attempts; it does not change the original idempotency fingerprint.

Errors return `{"error":"message"}`: 400 invalid request, 403 untrusted Host/Origin, 404 unknown route/job, 409 conflicting key or inactive token/state, 413 body size, 415 content type, 503 temporary storage failure. Unknown JSON fields are ignored. A repeated successful acknowledgment returns 409 because its lease has already ended. Clients should preserve their own completion receipt.
