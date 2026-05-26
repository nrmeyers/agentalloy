# Handoff: Sunset Tier 1 / Tier 3 Harness Terminology

## Context

The README and several internal docs still describe harnesses as **Tier 1** (per-turn hooks) or **Tier 3** (no hooks; file-watching sidecar). PR #8 made this framing obsolete by adding a universal **proxy** that intercepts every LLM request from any harness that honors a custom API base URL — so per-turn injection no longer depends on hook APIs.

The remaining operational distinction is one binary question:

> **Can the harness's LLM traffic be intercepted by the AgentAlloy proxy?**

- **Yes** → "proxy-wired" — config points the harness at `http://localhost:<port>/v1`, proxy injects context on every request.
- **No** → "sidecar" — harness routes through its own backend (Cursor, Windsurf, GitHub Copilot, Gemini CLI); AgentAlloy falls back to a static rules file kept current by a file-watching sidecar.

## Why the watcher code must stay

A previous suggestion to delete the watcher entirely was wrong. The spec at `.hermes/proxy_spec.md` (items A1, A8) confirms that **Gemini CLI** ignores `OPENAI_*` / `ANTHROPIC_*` env vars (talks to Google) and **Windsurf** has no first-party base-URL override. Cursor and GitHub Copilot route through their own services. For these four, the watcher is the only mechanism to react to phase/contract changes.

## Verified codebase state (start here before changing anything)

Branch context: do this on a **new branch** off `fix-copilot-pr8-feedback` (or wherever PR #8's proxy wiring lives). On `main` (HEAD `acfc9a9`), PR #8 isn't merged and the proxy-wiring functions don't exist.

**Proxy-wiring functions** in `src/agentalloy/install/subcommands/wire_harness.py` (dispatch at `_wire_proxy`, ~L794-835):

| Harness | Function | Config target |
|---|---|---|
| `continue-closed`, `continue-local` | `_wire_proxy_continue` | `~/.continue/config.yaml` `apiBase` |
| `aider` | `_wire_proxy_aider` (L893) | `.aider.conf.yml` `openai-api-base` |
| `hermes-agent` | `_wire_proxy_hermes_agent` (L942) | `~/.hermes/config.yaml` `custom_providers` (user) or `AGENTS.md` (repo) |
| `opencode` | `_wire_proxy_opencode` (L1013) | `.opencode/.agentalloy-env` `OPENAI_API_BASE` |
| `claude-code` | `_wire_proxy_claude_code` | `~/.agentalloy/claude-code-env.sh` `ANTHROPIC_BASE_URL` |
| `cline` | `_wire_proxy_cline` (L1115) | `.cline/settings.json` `apiBaseUrl` |
| (fallback) | `_wire_proxy_instruction` (L1156) | Just writes an instruction file; does NOT intercept traffic. Hit for cursor/windsurf/github-copilot/gemini-cli. |

**Stale Tier 3 set** in `src/agentalloy/watch/watcher.py:29`:
```python
TIER3_HARNESSES = frozenset(
    {"cursor", "windsurf", "github-copilot", "cline", "gemini-cli", "aider"}
)
```
Copilot's PR #8 review explicitly flagged that `aider` got proxy wiring and shouldn't be in this set. Same applies to `cline`. The correct sidecar set is `{cursor, windsurf, github-copilot, gemini-cli}`.

## Approach (per advisor guidance)

1. **Split into two commits** for easier review:
   - Commit 1: code rename + CLI flag deprecation alias + test renames
   - Commit 2: docs rewrite + file renames
2. **Only one breaking surface**: the CLI flag `--acknowledge-tier3`. Add `--acknowledge-sidecar` as the new canonical name and keep `--acknowledge-tier3` as a hidden alias (`help=argparse.SUPPRESS`) writing to the same `dest`.
3. **Naming**: use **"proxy-wired"** and **"sidecar"**. Avoid "non-routable" (ambiguous).
4. Internal symbols (`TIER3_HARNESSES`, `_wire_tier3_watcher_config`, `_tier3_harnesses` local vars, doc filenames, test filenames) — safe to rename freely.

## File-by-file changes

### Phase 1: Code

#### `src/agentalloy/watch/watcher.py`
- Replace module docstring:
  ```
  """File-system watcher loop for sidecar harnesses.

  Sidecar harnesses are those whose LLM traffic cannot be intercepted by the
  AgentAlloy proxy (they ignore base-URL overrides or route to their own
  backends). The watcher keeps their static rules files in sync with the
  current project phase and contract state.
  """
  ```
- Rename `TIER3_HARNESSES` → `SIDECAR_HARNESSES`, remove `cline` and `aider`:
  ```python
  SIDECAR_HARNESSES = frozenset(
      {"cursor", "windsurf", "github-copilot", "gemini-cli"}
  )
  ```
- Only one usage of the constant in the codebase — safe rename.

#### `src/agentalloy/watch/__init__.py`
- Replace `"""AgentAlloy file-watching sidecar for Tier 3 harnesses."""` with `"""AgentAlloy file-watching sidecar for harnesses that can't be proxy-wired."""`.

#### `src/agentalloy/watch/regenerators.py`
- First line of docstring: `"""Per-harness rules-file regenerators for Tier 3 harnesses.` → `"""Per-harness rules-file regenerators for sidecar harnesses.`
- Keep the registered regenerators for `cline` and `aider` — they're still used by `--legacy` mode.

#### `src/agentalloy/install/subcommands/watch.py`
- Module docstring line 1: `"""``agentalloy watch`` — Tier 3 file-watching sidecar.` → `"""``agentalloy watch`` — file-watching sidecar for non-proxy-wired harnesses.`
- `_detect_harness()` (L55-68): remove `"aider"` from the tuple at L64:
  ```python
  if h in ("cursor", "windsurf", "github-copilot", "gemini-cli"):
  ```
- Error message at L88 (the `--harness` required message): remove `|aider` from the list.
- Argparse help text (L181): `"Tier 3 file-watching sidecar..."` → `"File-watching sidecar for non-proxy-wired harnesses..."`.
- `--harness` arg help (L186): update to reference sidecar harness names.

#### `src/agentalloy/install/subcommands/wire_harness.py`
- Local var rename and section comment around L531-534:
  ```python
  # For sidecar harnesses (can't be proxy-wired), write watcher config and print guidance
  _sidecar_harnesses = frozenset({"cursor", "windsurf", "github-copilot", "gemini-cli"})
  if harness in _sidecar_harnesses:
      _wire_sidecar_watcher_config(harness, root)
  ```
- Section header comment around L582-584:
  ```
  # ---------------------------------------------------------------------------
  # Sidecar watcher wiring (harnesses that can't be proxy-wired)
  # ---------------------------------------------------------------------------
  ```
- Rename function L587: `_wire_tier3_watcher_config` → `_wire_sidecar_watcher_config`. Update docstring to "Write watcher config and print sidecar guidance. Soft-fail."
- Rewrite the user-facing print block at L605-616 to use "sidecar" terminology and reference `docs/sidecar-experience.md` instead of `docs/tier3-experience.md`. Replace "Tier 3 harnesses do not support per-turn hooks..." with "{harness} cannot be proxy-wired (it does not honor base-URL overrides for the AgentAlloy proxy)..."

#### `src/agentalloy/install/subcommands/simple_setup.py`
- Dataclass field L72: `acknowledge_tier3: bool = False` → `acknowledge_sidecar: bool = False`.
- Block at L991-1015 (the sidecar harness guardrail): rename `_tier3_harnesses` → `_sidecar_harnesses`, `tier3_msg` → `sidecar_msg`, `cfg.acknowledge_tier3` → `cfg.acknowledge_sidecar`. Rewrite messaging to reference proxy interception and `docs/sidecar-experience.md`. Sample replacement:
  ```python
  sidecar_msg = (
      f"\n  [yellow]Sidecar harness selected: {cfg.harness}[/yellow]\n"
      "  This harness cannot be proxy-wired (it does not honor OpenAI/Anthropic\n"
      "  base-URL overrides). AgentAlloy falls back to a static rules file kept\n"
      "  current by a file-watching sidecar. System skill enforcement is\n"
      "  advisory-only; phase transitions require the watcher to be running.\n"
      "  See docs/sidecar-experience.md for the full picture."
  )
  ```
  Prompt label: `"  Continue with sidecar harness?"` instead of `"  Continue with Tier 3?"`.
- Argparse (L1295-1301): replace the single `--acknowledge-tier3` arg with two:
  ```python
  p.add_argument(
      "--acknowledge-sidecar",
      action="store_true",
      default=False,
      dest="acknowledge_sidecar",
      help="Acknowledge sidecar harness limitations (required for non-interactive setup of cursor/windsurf/github-copilot/gemini-cli).",
  )
  # Deprecated alias; preserved for backward compatibility.
  p.add_argument(
      "--acknowledge-tier3",
      action="store_true",
      default=False,
      dest="acknowledge_sidecar",
      help=argparse.SUPPRESS,
  )
  ```
- `_run_from_args` at L1324: `acknowledge_tier3=getattr(args, "acknowledge_tier3", False)` → `acknowledge_sidecar=getattr(args, "acknowledge_sidecar", False)`.

#### `tests/test_setup_tier3.py`
- Rename internal refs: `acknowledge_tier3` → `acknowledge_sidecar`, `_tier3` → `_sidecar`, test function names `test_tier3_*` → `test_sidecar_*`, class/docstring text "Tier 3" → "sidecar". Update prompt assertion to look for "Continue with sidecar harness".
- Update import to `_wire_sidecar_watcher_config`.
- File rename is optional but cleaner: `tests/test_setup_tier3.py` → `tests/test_setup_sidecar.py`.

#### `tests/install/test_tier3_watcher.py`
- Rename class `TestTier3WatcherBehavior` → `TestSidecarWatcherBehavior`. Rename test methods. Update patch targets to `_wire_sidecar_watcher_config`. Update docstrings.
- File rename optional: `test_tier3_watcher.py` → `test_sidecar_watcher.py`.

### Phase 2: Docs

#### Delete + replace `docs/tier3-experience.md` → `docs/sidecar-experience.md`
- Full rewrite of the document. Lead with: "The AgentAlloy proxy intercepts LLM traffic from harnesses that honor a custom API base URL... A few harnesses can't be proxy-wired... so AgentAlloy falls back to writing a static rules file..."
- Include a "Which Harnesses Are Sidecar-Only" table with the four members and the reason each can't be proxy-wired (cite spec items A1/A8 reasoning).
- Capability matrix should compare "Proxy-wired" vs "Sidecar" columns, not "Tier 1" vs "Tier 3".
- Note that the `cline` and `aider` regenerators still exist for `agentalloy wire --legacy` users.
- Keep all the watcher-architecture, setup, CLI, troubleshooting content from the original.

#### Delete + replace `docs/harness-tier-spec.md` → `docs/harness-classification.md`
- No inbound refs to the old filename (verified via grep), so safe to rename freely.
- Rewrite as a two-category spec (proxy-wired / sidecar) with a single classification question: "Does the harness honor a custom API base URL that can be pointed at `http://localhost:<port>/v1`?"
- Include a Capability Matrix, current membership for each category, and a History section explaining the Tier 1/2/3 collapse (former Tier 2 — Continue / Hermes / OpenCode — now routes through proxy and gets per-turn injection; former Tier 1 vs Tier 3 distinction is moot because the proxy handles per-turn injection for any proxy-wired harness).

#### `README.md` — four sections need rewriting:
1. **L235 heading** `### Wired into a Tier 1 harness (full integration)` → `### Wired into a proxy-routable harness (full integration)`. Replace hook-centric paragraph with proxy-centric: "If your harness honors a custom API base URL... AgentAlloy points it at the local proxy. Every LLM request flows through the proxy..."
2. **L243 heading** `### Wired into a Tier 3 harness (sidecar)` → `### Wired into a sidecar harness`. Rewrite to: "A few harnesses (Cursor, Windsurf, GitHub Copilot, Gemini CLI) route through their own backends and can't be intercepted..."
3. **L270-288 Harness support section** — replace the "Tier classification depends entirely on whether the harness exposes a hook mechanism..." paragraph and the Tier 1 / Tier 3 capability table with a "Proxy-wired vs Sidecar" version. Update the link from `docs/tier3-experience.md` to `docs/sidecar-experience.md`, and add a link to `docs/harness-classification.md`. Membership lists:
   - **Proxy-wired**: Claude Code, Continue.dev, Aider, Cline, OpenCode, Hermes Agent
   - **Sidecar**: Cursor, Windsurf, GitHub Copilot, Gemini CLI
4. **L333, L335** CLI command table descriptions — `Tier 3 fallback` → `sidecar fallback`; `Wired by Tier 1 harnesses as a hook` → `Invoked by the proxy per request (proxy-wired harnesses)`.
5. **L349 `**Tier 3 sidecar**` heading** → `**Sidecar (for harnesses that can't be proxy-wired)**`. Update table row text correspondingly.
6. **L473-474 ASCII diagram** — replace `Tier 1: hook stdout → agent next turn` / `Tier 3: file watcher rewrites rules file` with `Proxy-wired: injected into next LLM request` / `Sidecar: file watcher rewrites rules file`.

(Note: the `tier-grouped` mention at README L96/L98 and `9 tiers` at L420 refer to **pack tiers** — a separate, valid concept. Leave them alone.)

#### `docs/operator.md`
- L107: rewrite "The signal layer runs in Tier 1 harnesses via per-turn hooks. In Tier 3 harnesses..." to describe the proxy/sidecar split. Update link to `sidecar-experience.md`.
- L109-117 "Hooks" subsection — rename to "Proxy interception" and rewrite content. The current hook-centric description is no longer accurate.
- L119-121 "Sidecar" subsection — keep concept, update wording from "Tier 3 harnesses" to "harnesses that can't be proxy-wired". Update doc link.
- L123-131 "Tiers" subsection — rename to "Classification", rewrite as two-category. Add link to `docs/harness-classification.md`.
- L226 heading `### Watcher Config (Tier 3)` → `### Watcher Config (sidecar harnesses)`.
- L313 (link list) — update `[Tier 3 Experience](tier3-experience.md)` → `[Sidecar Experience](sidecar-experience.md)`. Add `[Harness Classification](harness-classification.md)`.

#### `docs/profiles-and-overrides.md`
- L153: `The Tier 3 sidecar watcher is profile-aware:` → `The sidecar watcher (used for harnesses that can't be proxy-wired) is profile-aware:`
- L161: `See \`docs/tier3-experience.md\`` → `See \`docs/sidecar-experience.md\``

#### `docs/install/harness-catalog.md`
This file is the most stale — it already has partially-updated content. Full rewrite of the **"Full Harness List"** section (around L56-124):
- Drop the "Tier 1 / Tier 3 / Non-Tiered" three-section structure.
- Replace with two sections: **"Proxy-wired (default)"** and **"Sidecar (no proxy interception)"** plus a small **"Other"** section for `manual` and `mcp-only`.
- Proxy-wired table columns: Harness | Proxy Config File | Notes. Members: claude-code, continue-closed/local, aider, hermes-agent, opencode, cline.
- Sidecar table columns: Harness | Target File | Reason proxy is not available. Members: cursor, windsurf, github-copilot, gemini-cli.
- Move the "Claude Code hooks" / "Continue.dev hooks" details into a "Legacy hook mode" callout under the proxy-wired section, prefaced with the note that `--legacy` still installs them.
- Note that the `cline` / `aider` regenerators still exist for `--legacy` mode but aren't needed in the default proxy-wired flow.
- L179: `Used by Tier 3 regenerator functions...` → `Used by sidecar regenerator functions...`.

### Verification step (run before committing)

After all edits, this command should return only the deprecated CLI alias and the history paragraph in `harness-classification.md`:

```bash
grep -rniE "tier ?[13]|tier-[13]|tier3|tier1" --include="*.md" --include="*.py" \
  src/agentalloy/ tests/ docs/ README.md \
  | grep -v "_packs/" | grep -v "lint_tags_mechanical" | grep -v "skill_tier" \
  | grep -v "ASSEMBLY_TIER" | grep -v "assembly_tier" | grep -v "retrieval_tier"
```

Expected remaining hits (all intentional):
- `src/agentalloy/install/subcommands/simple_setup.py` — the `--acknowledge-tier3` deprecated alias
- `docs/harness-classification.md` — History section line explaining the rename

Also run:
```bash
grep -rn "tier3-experience\|harness-tier-spec" --include="*.md" --include="*.py"
```
Expected: zero hits (all references updated to the new filenames).

### Test suite

```bash
.venv/bin/python -m pytest tests/ -x --no-header -q
```
The original run with these edits passed 502 tests.

## What NOT to change

- Pack tier classification (`tier:` key in `pack.yaml` files, `skill_tier.py`, `TAG_POLICY_BY_TIER` in `ingest.py`, README references to "9 tiers" and "tier-grouped" pack listing). That's a separate concept (Foundation / Languages / Frameworks pack groupings) and is not affected.
- `assembly_tier`, `retrieval_tier`, `ASSEMBLY_TIER` constants in `orchestration/compose.py`, `storage/vector_store.py`, `telemetry/writer.py`. Unrelated.
- The watcher itself (`watch/watcher.py`, `watch/regenerators.py` registry). It's load-bearing for the four genuine sidecar harnesses.
- The `cline` and `aider` entries in `regenerators.py`'s registry dict. They're still needed for `--legacy` mode users.

## Commit plan

```bash
git switch fix-copilot-pr8-feedback   # or wherever PR #8's proxy wiring lives
git switch -c sunset-harness-tier-terminology

# Phase 1: code
# ...edits to src/ and tests/...
git add src/agentalloy/watch/ src/agentalloy/install/subcommands/{watch,wire_harness,simple_setup}.py tests/test_setup_tier3.py tests/install/test_tier3_watcher.py
git commit -m "Rename TIER3 harness terminology to 'sidecar' in code

The proxy redesign in PR #8 made the Tier 1 / Tier 3 split obsolete —
proxy-wired harnesses get per-turn injection regardless of hook support.
The remaining distinction is whether traffic can be intercepted at all.

- TIER3_HARNESSES -> SIDECAR_HARNESSES (and drop cline + aider, which
  got proxy wiring in PR #8 — Copilot review on PR #8 flagged this)
- _wire_tier3_watcher_config -> _wire_sidecar_watcher_config
- --acknowledge-tier3 -> --acknowledge-sidecar (old flag kept as hidden
  deprecated alias)
- Test/docstring/comment wording updates"

# Phase 2: docs (and file renames)
# ...edits to README.md, docs/...
git add README.md docs/
git commit -m "Sunset Tier 1 / Tier 3 terminology in docs

Rewrite README harness support section, operator.md, and harness-catalog
in terms of 'proxy-wired vs sidecar' instead of the obsolete tier model.

- docs/tier3-experience.md -> docs/sidecar-experience.md (full rewrite)
- docs/harness-tier-spec.md -> docs/harness-classification.md (rewrite
  as two-category spec)
- README, operator.md, profiles-and-overrides.md, harness-catalog.md:
  update terminology and cross-refs"
```
