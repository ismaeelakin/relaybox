# Architecture and failure analysis

The store owns queue correctness. The HTTP adapter validates transport boundaries. The TypeScript console renders data with `textContent`, never with payload-derived HTML. Generated JavaScript is committed so a Python-only checkout is runnable.

## Transaction boundaries

Each mutation opens its own SQLite connection, enters `BEGIN IMMEDIATE`, validates current state, writes, and commits. Exceptions roll back. A competing writer waits up to ten seconds before the adapter returns a retryable storage error. WAL permits readers alongside a writer. Snapshots read counts and rows in one transaction.

Enqueue checks `(queue, idempotency_key)` inside the write transaction, compares a SHA-256 fingerprint of canonical JSON plus delay and retry budget, then inserts. A unique constraint also encodes the invariant in storage. SHA-256 is used for identity comparison, not secret protection.

Claim first recovers expired leases, then selects an available job ordered by availability, creation time, and ID. It increments attempts and creates a random token before committing. Acknowledgment and failure require the active token and an unexpired lease in the same write transaction. Tokens are omitted from list/enqueue responses and logs.

## Failure matrix

| Failure point | Persisted outcome | Recovery |
| --- | --- | --- |
| Producer disconnects before response | Job may already exist | Retry identical request with same key |
| Worker crashes before side effect | Lease remains until deadline | Next claim requeues or dead-letters |
| Worker crashes after side effect | Side effect may already exist | Consumer deduplication required |
| Slow worker acknowledges after expiry | 409 conflict; no state change | Do not assume the external work was undone |
| Final attempt fails or expires | Dead letter | Operator inspects cause before replay |
| Process restarts | SQLite retains jobs | Restart using the same DB path |
| Clock moves | Lease/delay timing may shift | Use a synchronized host clock; no multi-host guarantee |
| DB filesystem fails | Storage operation fails | Restore from backups; no automatic repair |

The project uses persisted wall-clock timestamps because leases survive process restarts. It does not model arbitrary clock jumps, machine suspension, or network filesystems. Keep the database on a local disk. SQLite durability still depends on operating system and hardware behavior.

## Security and deployment boundary

Loopback binding, exact Host checks, JSON-only POST requests, Origin checks, no CORS headers, fixed static routes, CSP, and bounded bodies reduce accidental browser-to-localhost writes. They do not create authentication. Local processes can call this API, and JSON payloads and error strings are plaintext in the database. The Python development server has no bounded worker pool, TLS termination, quotas, or internet deployment support.

## What to measure next

Before expanding scope, measure enqueue/claim p50 and p99 under mixed reads/writes, writer wait times, disk growth, and crash recovery behavior. No throughput claim is made by this release. A production adapter would also need authenticated workers, request cancellation, connection limits, schema migrations, backups, retention, and operational metrics.
