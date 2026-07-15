# Service-Mediated Corpus Ingest

`POST /corpus/ingest-pack` lets the CLI write the **live** corpus while the
service is running — native or container — instead of requiring it to be stopped
(native) or being unreachable inside the data volume (container). It is the
durable fix for #390, which shipped honest failures in v6.7.0 and the routing in
v6.8.0.

## Why it exists

DuckDB is single-writer per file. A running service holds the store, so a
host-side CLI writer can never acquire the lock (native), and a container's
corpus lives inside the `agentalloy-data` volume a host CLI can't reach at all.
The fix routes the write through the one process that legally holds the writer:
the service itself, using the same `store.released()` + `install_local_pack` +
`refresh_runtime_cache` recipe the web wizard install already uses.

## Flow

1. The CLI (`lessons promote`, `install-pack <dir>`) generates the pack on the
   host, then `decide_corpus_write_route()` (`install/corpus_write_route.py`)
   picks a route from live deployment state:
   - **service reachable** → push the pack bytes to `/corpus/ingest-pack`;
   - **service down, corpus host-writable** → today's direct `install_local_pack`;
   - **neither** → the honest #390 block message.
2. The endpoint materializes the bytes to a temp dir, runs the dedup probe
   **server-side** (against the live corpus — a container corpus can't be probed
   from the host), then installs under `store.released()` and reloads the cache.
3. The result dict is returned verbatim; the CLI maps it onto the existing
   result actions (`promoted` / `duplicate_refused` / `install_failed` / …).

`install-packs` is **intentionally not routed** — it is the bulk-bootstrap path
and already writes the corpus with the service up via its stop→ingest→restart
container guard. See `docs/followups.md`.

## Security posture (the AC-7 decision)

Corpus mutation is a new authority on the service port, and a container publishes
`0.0.0.0:47950` to the LAN/tailscale. The proxy `/proj/{token}` scheme is **not**
auth — the token is `base64url(realpath(project_dir))`, publicly derivable — so it
cannot guard this endpoint.

The guard is a **shared ingest secret** (`install/ingest_secret.py`):

- The **host is source of truth.** The secret is minted once and persisted to
  `${XDG_CONFIG_HOME}/agentalloy/ingest-secret` (0600). A container receives the
  same value via the `AGENTALLOY_INGEST_SECRET` env var injected at `podman run`
  (so the in-container service and the host CLI converge without the host reading
  inside the volume).
- Every request must carry `X-AgentAlloy-Ingest-Token`; the service compares it in
  constant time (`secret_matches`) and 401s otherwise. The service **never mints**
  — an unconfigured service rejects everything (fail-closed).
- `AGENTALLOY_CORPUS_INGEST=0` disables the route entirely (404).

Alternatives rejected: the wizard's CSRF header (an attacker just sets it) and
loopback-only enforcement (would block the legitimate host→container call, which
arrives via the rootlessport forwarder with a gateway source IP). Full rationale:
`docs/design/service-mediated-corpus-ingest/approach.md` (D3).
