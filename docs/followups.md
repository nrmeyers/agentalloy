# Follow-ups

Deferred, non-blocking items. Each names the trigger context so it can be picked
up later without re-deriving it.

## Corpus ingest

- **install-packs is intentionally not service-routed.** The service-mediated
  ingest work (#390) routed `lessons promote` and `install-pack` through
  `POST /corpus/ingest-pack`, but left `install-packs` on its stop→ingest→restart
  container guard, which already writes the corpus with the service up. Rationale:
  install-packs is the bulk-bootstrap path (highest-stakes — a bug breaks the whole
  corpus), often runs before the service is serving or during an upgrade cycle, and
  the only gain from routing is removing a brief restart blip. If that blip ever
  becomes an operational complaint, route it per-pack with reembed batched to the
  final pack (mirror `install_local_pack(run_reembed=...)`), no restart. _Decided
  with user, 2026-07-11._

## Retrieval quality

- **Snowflake/Iceberg domain fragments leak into unrelated build injections.**
  Observed 2026-07-11 while composing for the `ingest-secret-provisioning` build
  work-item (a secret/config task with no data-warehouse surface) — the injected
  domain fragments were snowflake/iceberg. Off-topic corpus fragments should not
  surface for a task whose tags are `[install-secret, container-bootstrap]`. Likely
  the same free-text-compose / benchmark-corpus-pollution class tracked in the
  contract-path-dormant and benchmark-fidelity threads (every phase composes
  free-text with high filler; the deterministic tag-scoped contract path is
  effectively unwired in prod). Investigate after the service-mediated-corpus-ingest
  feature ships. _Deferred by user, 2026-07-11._
