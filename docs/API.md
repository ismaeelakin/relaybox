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

## Retrying an enqueue safely

Send an `idempotency_key` when a producer may repeat a request after a timeout.
For example, POST this body to `/api/jobs`:

```json
{
  "queue": "reports",
  "payload": {"task": "daily-report"},
  "idempotency_key": "daily-report-demo",
  "delay": 0,
  "max_attempts": 3
}
```

On a fresh database, the first request returns `201` with `created: true`.
Repeating it returns `200` with `created: false` and the same job ID. Object key
order and JSON whitespace do not matter: payloads are compared using canonical
JSON. Omitting `delay` and `max_attempts` is equivalent to sending their defaults
of `0` and `3`.

Reusing that key in the same queue with a different payload, `delay`, or
`max_attempts` returns `409`; it does not update the existing job. Use a new key
for a genuinely new request. The same key in a different queue identifies a
separate job.

Acknowledgment and dead-letter replay do not free the key. Repeating an enqueue
after the job finishes still refers to the original job, rather than scheduling
another execution. Manual replay resets the job's attempt budget but preserves
its original idempotency fingerprint.
