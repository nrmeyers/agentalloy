# Manual smoke checklist — IDE / sidecar harnesses

The harness e2e matrix (`uv run pytest -m harness_e2e -n0`) live-verifies
every headless-drivable harness. The four below cannot be driven headless —
they need a human once per release (or after a harness updates itself).
Each check is ~3 minutes. Record results in the release notes.

Setup for all checks: a wired repo (`agentalloy wire --harness <name>`) with
the service running (`curl -s localhost:47950/health`).

## cursor (sidecar — rules file)

1. Wire; confirm `.cursor/rules/agentalloy.mdc` (with `.cursor/` present; else
   a marker block in `.cursorrules`) exists and contains the current phase.
   The watcher refreshes the same file.
2. Open the repo in Cursor, start `agentalloy watch start --harness cursor`.
3. Ask the agent "what phase is this project in per your rules?" — answer
   should match `.agentalloy/phase`.
4. `agentalloy phase set build` (or advance normally); within ~1s the rules
   file should regenerate — re-ask and confirm the agent sees the new phase.

## windsurf (sidecar — rules file)

Same four steps against `.windsurf/rules/agentalloy.md` / `.windsurfrules`.

## cline (proxy — CLI covered by the e2e matrix; this checks the IDE side)

The headless cline CLI is live-verified by the matrix against the same
`~/.cline/data/settings/providers.json` store the extension reads. The manual
check confirms the VS Code extension honors it too:

1. Wire; confirm `~/.cline/data/settings/providers.json` carries the
   `openai-compatible` provider (`baseUrl` → the proxy) and
   `lastUsedProvider: openai-compatible`.
2. Open VS Code + Cline in the wired repo; send one prompt.
3. Confirm the turn completes AND the service telemetry shows a
   `proxy_composed`/`proxy_passthrough` row (`agentalloy status` or the web
   UI) — that proves the extension used the store, not its cached
   globalState provider.

## antigravity / gemini-cli (sidecar — GEMINI.md)

1. Wire; confirm `GEMINI.md` carries the marker-bounded AgentAlloy block.
2. Run the Antigravity CLI in the repo (Google login required); ask what the
   project's workflow phase is — answer should match the block.
3. Advance the phase with the watcher running; confirm the block regenerates.

## Recording

Add one line per harness to the release PR:
`manual-smoke: cursor ✅ windsurf ✅ cline ✅ antigravity ✅ (vX.Y.Z, date)`.
A harness nobody smoked in the last two releases gets demoted to
"unverified" in the README table — the matrix and this checklist together
define what "supported" means.
