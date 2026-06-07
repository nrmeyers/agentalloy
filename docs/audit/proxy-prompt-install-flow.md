# Audit: Proxy Prompt & Preflight Flow — When prompts are shown and why failures occur

## Problem Statement

A user selecting `github-copilot` as their harness was still presented with the "Upstream LLM (proxy target)" prompt, then hit a preflight failure for missing Ollama. The proxy prompt is shown unconditionally in interactive mode, regardless of harness choice.

---

## 1. Harness Types and Proxy Relevance

### Proxy-able harnesses (need upstream LLM)
These harnesses route their LLM traffic through the AgentAlloy proxy. The proxy needs to know where to forward requests.

- `claude-code` — writes CLAUDE.md with proxy URL
- `hermes-agent` — writes SOUL.md/AGENTS.md with proxy URL
- `opencode` — writes `.opencode/system-prompt.md` with proxy URL
- `aider` — writes `.agentalloy-aider-instructions.md` with proxy URL
- `cline` — writes `.clinerules` with proxy URL
- `continue-closed` / `continue-local` — writes `.continuerc.json` with proxy URL
- `codex` — writes `~/.codex/config.toml` with proxy URL
- `openclaw` — writes `~/.openclaw/plugins.json` with proxy URL

### Sidecar harnesses (cannot use proxy)
These harnesses cannot be proxy-wired. Their traffic does NOT flow through the AgentAlloy proxy.

Defined in `src/agentalloy/install/__init__.py:PROXY_UNABLE_HARNESSES`:
- `cursor` — markdown injection only
- `windsurf` — markdown injection only
- `github-copilot` — markdown injection only
- `gemini-cli` — markdown injection only

### Manual harness
- `manual` — prints instructions to stdout, no wiring

**Conclusion**: The proxy prompt should only be shown when the user selects a proxy-able harness. For sidecar harnesses, the upstream LLM prompt is irrelevant because the harness never sends traffic through the proxy.

---

## 2. Current Flow (BROKEN)

```
Step 1: Prompt runner (ollama/lm-studio/llama-server)
Step 2: Prompt model
Step 3: Prompt port
Step 4: Prompt mode (persistent/manual)
Step 5: Prompt packs
Step 6: Prompt harness
Step 7: Prompt hardware
Step 8: Upstream LLM prompt  ← SHOWN UNCONDITIONALLY (line 1675)
  - Base URL [http://localhost:11434 for Ollama, https://api.openai.com for OpenAI]
  - Model name []
  - API key []
Step 9: Summary + Confirm
Step 10: Preflight (early)
Step 11: Preflight (runner)  ← FAILS HERE if ollama not installed
Step 12: Write .env
Step 13: Write upstream LLM config
Step 14: Pull model
...
```

**Problems**:
1. Step 8 is unconditional — shown even for sidecar harnesses that don't use the proxy
2. Preflight (runner) runs AFTER the user confirms, so the user already invested time in the proxy prompt
3. The ollama preflight checks (`ollama_present`, `ollama_reachable`) are only relevant when runner == "ollama"
4. The upstream LLM prompt is only relevant when harness is proxy-able

---

## 3. Root Cause

**File**: `src/agentalloy/install/subcommands/simple_setup.py`

**Line 1674-1676**:
```python
# 8. Upstream LLM
if not cfg.non_interactive:
    _prompt_upstream(cfg)
```

This block has NO condition checking `cfg.harness`. It runs for every interactive setup, regardless of whether the selected harness can use the proxy.

The harness is available at this point (prompted in Step 6), so a simple guard would fix this:
```python
if not cfg.non_interactive and cfg.harness not in PROXY_UNABLE_HARNESSES:
    _prompt_upstream(cfg)
```

---

## 4. Preflight Flow Analysis

**File**: `src/agentalloy/install/subcommands/preflight.py`

### Runner-phase checks (lines 802-849)
The runner-phase preflight is runner-specific:
- `ollama` → checks `ollama_present` + `ollama_reachable`
- `llama-server` → checks `llama_server_present` (no reachability check)
- `fastflowlm` → checks `fastflowlm_present`
- unknown → warning only

### Why the user's preflight failed

The user selected:
- **Runner**: ollama
- **Harness**: github-copilot (sidecar, can't use proxy)

The preflight correctly identified that ollama is not installed (`ollama_present` failed) and not reachable (`ollama_reachable` failed). The preflight logic is correct — it's the **timing** that's the problem.

The user was prompted for upstream LLM settings (which are irrelevant for github-copilot) BEFORE encountering the preflight failure. The preflight failure is also correct but comes too late in the UX flow.

---

## 5. Recommended Fix

### 5.1 Guard the upstream LLM prompt

In `simple_setup.py`, line ~1674, add a harness check:

```python
# 8. Upstream LLM (only for proxy-able harnesses)
if not cfg.non_interactive and cfg.harness not in PROXY_UNABLE_HARNESSES:
    _prompt_upstream(cfg)
else:
    # Sidecar harnesses don't need upstream LLM config
    if cfg.harness in PROXY_UNABLE_HARNESSES:
        _print(f"  [dim]Harness '{cfg.harness}' is sidecar-only (no proxy wiring). Skipping upstream LLM prompt.[/dim]")
    elif cfg.non_interactive:
        _print(f"  [dim]Non-interactive mode — upstream LLM uses defaults (configurable later via env vars).[/dim]")
```

### 5.2 Move preflight BEFORE the proxy prompt (optional improvement)

Currently the order is:
```
Prompt harness → Prompt upstream LLM → Confirm → Preflight
```

A better order would be:
```
Prompt harness → Confirm → Preflight → Prompt upstream LLM (if needed) → Execute
```

This way, preflight failures are caught before the user invests time in the proxy prompt. However, this is a larger UX refactor.

### 5.3 Clarify preflight error messages for sidecar harnesses

When the user selects a sidecar harness, the preflight runner check is still relevant (they still need an embedding runner). But the error message should be clearer about what's needed:

```
Runner preflight failed:
  - ollama_present: brew install --cask ollama-app failed: auto-install disabled
  - ollama_reachable: GET http://localhost:11434/api/tags failed: Connection refused

Note: You selected github-copilot (sidecar harness). You still need ollama for embeddings,
but the AgentAlloy proxy is NOT used for LLM traffic.
```

---

## 6. Files Affected

| File | Lines | Change |
|------|-------|--------|
| `src/agentalloy/install/subcommands/simple_setup.py` | ~1674-1676 | Add harness guard to `_prompt_upstream()` call |
| `src/agentalloy/install/__init__.py` | 14-16 | `PROXY_UNABLE_HARNESSES` — already defined, just needs to be imported |

---

## 7. Edge Cases to Consider

1. **Non-interactive mode**: When `--non-interactive` is used, `_prompt_upstream()` is skipped entirely. The upstream URL/model come from `SetupConfig` defaults. This is fine — the defaults are harmless.

2. **User manually sets upstream LLM for a sidecar harness**: If a user explicitly sets `UPSTREAM_URL` via env vars even with a sidecar harness, the proxy will still be configured but unused. This is not harmful — it's just dead configuration.

3. **Harness changes during setup**: Not possible — the harness is selected once and not re-prompted.

4. **Container deployment**: Container deployment has its own preflight phase (`container`) that checks podman/docker. The harness/proxy logic is the same — container deployment doesn't change whether the proxy is needed.

---

## 8. Summary

**The proxy prompt should be conditional on harness type.** Sidecar harnesses (cursor, windsurf, github-copilot, gemini-cli) cannot use the proxy, so prompting for upstream LLM settings is confusing and wastes the user's time.

The preflight failure is correct but arrives after the user has already filled out an irrelevant form. Moving preflight before the proxy prompt would be a better UX, but the minimal fix is simply adding a harness guard to the upstream LLM prompt.
