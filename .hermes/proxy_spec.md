# UAHSI Verification Spec

A falsifiable checklist for an agent to verify the Universal AI Harness Skill Interceptor (UAHSI) implementation against the issues surfaced during spec review. Each item has a stable ID, the expected behavior, how to verify it, and a clear pass/fail criterion.

The agent should produce a report keyed by check ID with one of: **PASS**, **FAIL**, **N/A** (with justification), or **NEEDS HUMAN** (with the specific question).

---

## A. Harness Configuration (Section 3 of UAHSI spec)

### A1. Gemini CLI must not be configured via OpenAI/Anthropic env vars
**Expected:** `gemini-cli` is treated as Tier 3 (UI/config) or excluded. The Tier 2 shell-rc append must not claim to reroute Gemini CLI via `OPENAI_API_BASE` or `ANTHROPIC_BASE_URL`.
**Why:** Google's `gemini-cli` reads `GEMINI_API_KEY` / `GOOGLE_API_KEY` and talks to Google's Gemini API; neither OpenAI nor Anthropic base-URL variables affect its routing.
**Verify:** Read the CLI source/config. Confirm `gemini-cli` is absent from Tier 2 target list, or that the Tier 2 handler explicitly skips it.
**Pass:** No code path exports OpenAI/Anthropic env vars and claims this reconfigures Gemini CLI.
**Fail:** Gemini CLI is listed alongside Aider in Tier 2 with no separate handling.

### A2. Cline / Roo Code configuration path is correct
**Expected:** Cline configuration is not attempted via VS Code user `settings.json` keys like `cline.apiProvider` / `cline.openai`. These are not real settings keys. Cline stores provider config in its extension state (set via its in-panel UI / secret storage).
**Verify:** Search the codebase for the strings `cline.apiProvider` and `cline.openai`. Confirm the integration either (a) uses Cline's actual configuration surface, or (b) is downgraded to Tier 3 (UI instructions).
**Pass:** No writes to `settings.json` using fabricated `cline.*` keys.
**Fail:** Tier 1 mutator writes those keys and claims silent automation.

### A3. Continue config format handled
**Expected:** Continue integration handles `~/.continue/config.yaml` (current primary format) in addition to legacy `~/.continue/config.json`.
**Verify:** Inspect the Continue mutator. Confirm it detects which file exists and applies the equivalent `apiBase` mutation to each model entry in the appropriate format (YAML mutation if `config.yaml` is present).
**Pass:** Both formats supported, or the YAML format is the default with JSON as fallback.
**Fail:** Only `config.json` is read/written.

### A4. OpenCode provider block includes required `models` map
**Expected:** When the CLI writes to `~/.config/opencode/opencode.json`, each `provider.<id>` object includes a `models` map alongside `npm` and `options`. Without `models`, the provider does not surface selectable models in OpenCode.
**Verify:** Inspect the mutator output against OpenCode's current `opencode.json` schema. Confirm the written JSON includes at minimum one entry under `provider.custom_proxy.models`.
**Pass:** Written config includes `models` and OpenCode successfully lists the proxy provider's models in its picker.
**Fail:** Config matches the spec snippet exactly (no `models`) and OpenCode does not surface the provider.

### A5. `ANTHROPIC_BASE_URL` has no `/v1` suffix
**Expected:** `ANTHROPIC_BASE_URL` is set to `http://localhost:8000` (no `/v1`). Anthropic SDKs append `/v1/messages` to the base.
**Verify:** Grep for `ANTHROPIC_BASE_URL` across mutators (Claude Code settings, shell-rc, any docs/onboarding). Confirm no setter writes a value ending in `/v1`.
**Pass:** All occurrences are `http://localhost:8000` (or other host without `/v1`).
**Fail:** Any setter writes `http://localhost:8000/v1`.

### A6. Both `OPENAI_BASE_URL` and `OPENAI_API_BASE` exported
**Expected:** Tier 2 shell-rc append exports **both** `OPENAI_BASE_URL` (modern OpenAI SDK v1+) and `OPENAI_API_BASE` (legacy / LiteLLM-based tools like Aider). Setting only one will silently miss consumers.
**Verify:** Read the appended block in `~/.zshrc` / `~/.bashrc` after running setup. Confirm both env vars are present and identical.
**Pass:** Both variables exported with the same value.
**Fail:** Only one of the two is exported.

### A7. Shell detection handles login vs. interactive shells and bash profile variants
**Expected:** Shell detection does not rely solely on `$SHELL` (which reports the *login* shell on macOS and may not match the user's active interactive shell). Bash users on macOS commonly use `~/.bash_profile` rather than `~/.bashrc`.
**Verify:** Inspect detection logic. Confirm it: (a) handles `~/.bash_profile` on macOS for bash, (b) does not assume `$SHELL` equals the active shell, (c) optionally prompts the user if ambiguous.
**Pass:** Detection covers `.zshrc`, `.bashrc`, and `.bash_profile` with sensible per-OS defaults.
**Fail:** Detection writes only to `.bashrc` for any `$SHELL` containing `bash`, regardless of OS.

### A8. Windsurf onboarding language is accurate
**Expected:** Tier 3 onboarding text does not claim that Windsurf "natively forbids" third-party routing. The accurate framing: Windsurf does not expose a first-party base-URL override; Roo Code (or similar) is a workaround.
**Verify:** Read the printed instructions block for Windsurf.
**Pass:** Wording reflects "no first-party override" rather than active blocking.
**Fail:** Text asserts active prohibition.

### A9. `hermes-agent` reference is real
**Expected:** Any `hermes-agent` integration points to an actual distributable tool with verifiable provenance (repo, package, or internal product). I incorrectly flagged this as fictional earlier — `hermes-agent` does appear to exist in this environment (`~/hermes-agent`). The check is that the reference resolves to that real tool and that its env-var handling actually honors `OPENAI_API_BASE` / `ANTHROPIC_BASE_URL`.
**Verify:** Confirm the `hermes-agent` referenced is the same project at `~/hermes-agent`. Grep its source for `OPENAI_API_BASE` / `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` to confirm the env-var-based Tier 2 strategy actually works. If `hermes-agent` uses a config file instead, demote to Tier 1 or Tier 3.
**Pass:** Tool exists *and* honors the env vars Tier 2 exports.
**Fail:** Tool exists but ignores those env vars (Tier 2 silently does nothing for it).

### A10. Claude Code `ANTHROPIC_BASE_URL` honored caveat documented
**Expected:** Documentation acknowledges that some Claude Code enterprise/Team configurations route through Anthropic's gateway and may silently ignore `ANTHROPIC_BASE_URL`, so interception is not guaranteed for those installs. (Auth itself works because the proxy forwards to real `api.anthropic.com`.)
**Verify:** Search docs for any mention of this caveat. Confirm setup output warns the user if it cannot verify interception is active (e.g., after setup, a smoke-test request should round-trip through the proxy and the CLI should report success).
**Pass:** Caveat documented and/or smoke test detects when override is ignored.
**Fail:** Spec claims universal interception with no caveat.

---

## B. Proxy Layer (Section 4 of UAHSI spec)

### B1. `/v1/messages` mutation handles Anthropic payload shape
**Expected:** For `POST /v1/messages`, the mutator treats `system` as a top-level field (string or content-block array), not as a `messages[]` entry with `role: "system"`. Context injection on the Anthropic path mutates the top-level `system` field; the OpenAI path mutates `messages[]` with `role: "system"`.
**Verify:** Inspect the mutation pipeline. Confirm a branch on endpoint/format, and that an end-to-end test against `/v1/messages` produces a payload where injected context lands in the top-level `system` field.
**Pass:** Anthropic and OpenAI payload shapes each have their own correct mutation branch.
**Fail:** A single code path appends a `role: "system"` message to Anthropic payloads.

### B2. Deterministic vs. vector retrieval framing is consistent
**Expected:** Internal terminology is consistent: either the routing engine is described as "deterministic" (rules / pattern matching / lookup tables) **or** the spec acknowledges that vector retrieval is included and is not strictly deterministic. The phrase "Deterministic Logic Engine" should not co-exist with "vector datasets" in the same component without explanation.
**Verify:** Read the router module's docstrings / README. Confirm naming and described behavior align.
**Pass:** Naming matches behavior; mixed retrieval is acknowledged.
**Fail:** "Deterministic" is claimed while vector similarity drives the routing decision.

### B3. Header passthrough preserves Anthropic + OpenAI auth
**Expected:** The proxy mirrors `x-api-key`, `anthropic-version`, and `authorization` from incoming to outgoing headers. Bearer tokens (Claude Code OAuth, OpenAI API keys) and Anthropic API keys all round-trip to the real upstream.
**Verify:** Unit test the header-construction function with three fixtures: (a) Anthropic `x-api-key` + `anthropic-version`, (b) OpenAI `Authorization: Bearer ...`, (c) Claude Code OAuth `Authorization: Bearer ...`. Confirm each comes out intact.
**Pass:** All three fixtures preserved.
**Fail:** Any of the three is dropped, renamed incorrectly, or truncated.

### B4. Header casing normalized on output
**Expected:** Outbound header names use a consistent convention (HTTP is case-insensitive, but mixing `Authorization` with `x-api-key` is a code-smell and can interact poorly with downstream middleware or logging). Pick canonical casing and apply uniformly.
**Verify:** Inspect the outbound header dict in `httpx` client construction.
**Pass:** Casing is consistent (all canonical or all lowercase).
**Fail:** Mixed casing as in the spec snippet.

### B5. Endpoints expose both `/v1/chat/completions` and `/v1/messages`
**Expected:** FastAPI app registers both `POST /v1/chat/completions` (OpenAI) and `POST /v1/messages` (Anthropic) and routes each to the correct upstream.
**Verify:** Hit each endpoint with a minimal payload; confirm the proxy forwards `/v1/chat/completions` to `api.openai.com` and `/v1/messages` to `api.anthropic.com`.
**Pass:** Both routes exist and forward correctly.
**Fail:** Either route is missing or misrouted.

### B6. Typo fix: "asynchronous HTTP client client"
**Expected:** Phrase in spec/docs reads "asynchronous HTTP client (`httpx.AsyncClient()`)" — no duplicated word.
**Verify:** Grep for `client client` in the repo's docs/comments.
**Pass:** No occurrences.
**Fail:** Phrase still duplicated.

---

## C. Distribution & Setup (Section 3 of UAHSI spec)

### C1. `pipx` distribution mode appropriate
**Expected:** Setup is distributed such that persistent shell mutations are not lost between runs. If `pipx run` is the documented entry point, persisted artifacts (rc-file appends, JSON mutations) are still present after the ephemeral environment exits.
**Verify:** Run `pipx run uahsi-cli setup` in a clean environment. Confirm rc-files and config files retain mutations after the `pipx run` cache is cleared.
**Pass:** Mutations persist.
**Fail:** Any persistent artifact is written to the ephemeral `pipx run` venv and lost.

### C2. Idempotent shell-rc mutations
**Expected:** Re-running setup does not duplicate the injected block in `~/.zshrc` / `~/.bashrc` / `~/.bash_profile`. The mutator checks for an existing marker (e.g., `# --- Injected by UAHSI Setup ---`) before appending.
**Verify:** Run setup twice. Diff the rc-file before and after the second run.
**Pass:** Second run is a no-op for the rc-file.
**Fail:** Block is duplicated.

---

## D. Security (Section 5 of UAHSI spec)

### D1. Listener bound to loopback only
**Expected:** Uvicorn/FastAPI host is bound to `127.0.0.1` (not `0.0.0.0`). No CLI flag or env var can override to a public interface without an explicit, documented opt-in.
**Verify:** Inspect server startup. Confirm host argument is hard-coded or defaulted to `127.0.0.1` and any override is documented and warned about.
**Pass:** Loopback-only by default; overrides gated.
**Fail:** Binds to `0.0.0.0` by default, or accepts arbitrary host without warning.

### D2. Local-caller authentication considered
**Expected:** Documentation acknowledges that on multi-user hosts (shared servers, devcontainers), any local process can hit the proxy and have its upstream key/token forwarded. Either (a) a local shared secret is enforced, or (b) the limitation is documented prominently.
**Verify:** Search docs for the multi-user caveat or for a local-auth mechanism (e.g., a token in `Authorization`/`x-uahsi-token` that the proxy validates before forwarding).
**Pass:** Either local auth implemented, or limitation documented.
**Fail:** Neither.

---

## E. Reporting Format

The verifying agent should return a table:

| ID  | Status | Evidence (file:line or test name) | Notes |
| --- | ------ | --------------------------------- | ----- |
| A1  | PASS / FAIL / N/A / NEEDS HUMAN | … | … |
| …   | …      | …                                 | …     |

Any **FAIL** must include the smallest code change needed to flip it to **PASS**. Any **NEEDS HUMAN** must include the specific question to escalate.
