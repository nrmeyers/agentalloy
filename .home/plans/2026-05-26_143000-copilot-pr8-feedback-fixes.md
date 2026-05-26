# Fix Copilot PR-8 Feedback — 10 Items

## Goal

Address all 10 comments Copilot raised on PR 8 (merged). Fixes span five categories:
data-loss bugs, security/robustness, test hermeticity, cosmetic cleanup, and docstring accuracy.

## Grouped by Priority

### Tier 1: Data-loss bugs (must fix)

#### Fix 1: OpenCode uninstall deletes entire system-prompt.md (Comment 3)

**Problem:** `_unwire_proxy_opencode` does `prompt_path.unlink()` on `.opencode/system-prompt.md`, but `_wire_proxy_opencode` uses `_inject_sentinel_block` which preserves existing user content. Uninstall destroys user data.

**Fix:**
- File: `src/agentalloy/install/subcommands/uninstall_proxy.py`, function `_unwire_proxy_opencode`
- Replace the `prompt_path.unlink()` with sentinel-based removal:
  1. Read the file
  2. Call `_remove_sentinel_block(content)`
  3. If result is empty/whitespace, `unlink()` the file
  4. Otherwise, `write_text()` the cleaned content
- Return `[prompt_path]` only if a change actually occurred

**Test:** Add a test in `tests/install/` that:
  - Creates `.opencode/system-prompt.md` with user content + sentinel block
  - Runs `_unwire_proxy_opencode`
  - Asserts user content is preserved, sentinel block is removed

---

#### Fix 4: Claude Code env file deleted without checking sentinel (Comment 4)

**Problem:** `_unwire_proxy_claude_code` does `env_path.unlink()` on `~/.agentalloy/claude-code-env.sh`, but `_wire_proxy_claude_code` preserves existing content via sentinel injection. Asymmetric.

**Fix:**
- File: `src/agentalloy/install/subcommands/uninstall_proxy.py`, function `_unwire_proxy_claude_code`
- Same pattern as Fix 1: read, `_remove_sentinel_block`, only unlink if empty, otherwise write back
- Keep the stderr message about `.bashrc/.zshrc` cleanup

**Test:** Same pattern — user content in env file survives uninstall.

---

### Tier 2: Bugs / Robustness

#### Fix 2: Aider proxy mode references non-existent instructions file (Comment 1)

**Problem:** `_wire_proxy_aider` writes `.aider.conf.yml` with `read: - .agentalloy-aider-instructions.md` but never creates that file in proxy mode. The legacy path does (it's the dedicated target), but proxy mode doesn't.

**Fix:**
- File: `src/agentalloy/install/subcommands/wire_harness.py`, function `_wire_proxy_aider`
- Option A (preferred): Remove the `read` lines from the proxy block. Proxy mode already configures `openai-api-base`, `openai-api-key`, and `model` — that's sufficient. The instructions file is only needed for the legacy (non-proxy) wiring where the full prompt is injected.
- Option B: Create the instructions file in proxy mode too (same as legacy path).
- I'd go with Option A — proxy mode doesn't need the instructions file; the proxy handles context injection server-side.

**Also update:** `_unwire_proxy_aider` — if we remove the `read` lines from proxy wiring, uninstall shouldn't try to delete the instructions file (it won't exist in proxy mode).

**Test:** Wire aider in proxy mode, verify `.aider.conf.yml` has proxy config but no `read` entry pointing to a missing file.

---

#### Fix 3: Streaming error path injects unescaped model into JSON (Comment 5)

**Problem:** Lines 256-264 in `proxy_anthropic_router.py` build SSE error payloads via f-strings with unescaped `model` variable. A model name containing quotes or backslashes breaks JSON/SSE framing.

**Fix:**
- File: `src/agentalloy/api/proxy_anthropic_router.py`, function `_stream_anthropic_response` (inside `event_generator`)
- Replace the f-string error payloads with `json.dumps()`:
  ```python
  msg_start_data = {
      "type": "message_start",
      "message": {
          "id": "msg_error",
          "type": "message",
          "role": "assistant",
          "content": [],
          "model": model,  # safe — json.dumps handles escaping
          "stop_reason": None,
          "stop_sequence": None,
          "usage": {"input_tokens": 0, "output_tokens": 0}
      }
  }
  yield f"event: message_start\ndata: {json.dumps(msg_start_data)}\n\n"

  error_data = {
      "type": "error",
      "error": {
          "type": "api_error",
          "message": f"Upstream returned HTTP {resp.status_code}"
      }
  }
  yield f"event: error\ndata: {json.dumps(error_data)}\n\n"
  ```
- Consider broadening the error check from `>= 500` to `!= 200` to handle 401/429/etc. in streaming mode (the non-streaming path already does this on line 379).

**Test:** Add a test with model names containing special characters (quotes, backslashes) and verify the SSE output is valid JSON.

---

#### Fix 5: _resolve_model forwards synthetic name upstream when upstream_model is unset (Comment 9)

**Problem:** `_resolve_model` returns `"agentalloy-proxy"` when `upstream_model` is None/empty, causing an opaque upstream error.

**Fix:**
- File: `src/agentalloy/api/proxy_router.py`, function `_resolve_model`
- Add explicit check: if `model == "agentalloy-proxy"` and `upstream_model` is falsy, raise or return a sentinel that triggers a clear 503 response
- Better approach: move the check into `_build_payload` (the caller), since that's where we already have access to settings. If the resolved model is `"agentalloy-proxy"`, raise a `ValueError` or return an error response directly.
- Actually, the simplest fix: in `_resolve_model`, if model is `"agentalloy-proxy"` and `upstream_model` is falsy, return `None` to signal the error. Then in `_build_payload`, check for `None` model and raise. Or just add the check in the route handler before calling `_build_payload`.

**Test:** Send request with model `"agentalloy-proxy"` when upstream_model is unset, verify clear 503 response.

---

### Tier 3: Test hermeticity

#### Fix 6: Tests write to ~/.agentalloy/ without mocking Path.home() (Comment 10)

**Problem:** `test_records_harness_in_state` and `test_records_files_written` call `wire_harness("claude-code", ...)` in default proxy mode, which writes to `~/.agentalloy/claude-code-env.sh`.

**Fix:**
- File: `tests/install/test_wire_harness.py`
- Add a conftest fixture that patches `pathlib.Path.home` to return a `tmp_path` subdirectory
- Apply it to the `TestState` class (and any other tests that exercise claude-code proxy wiring)
- Also check `test_all_valid_harnesses_accepted` — it uses `legacy=True` so it avoids proxy wiring, but verify it doesn't hit the claude-code proxy path

**Verification:** Run the test suite, confirm no files appear in `~/.agentalloy/` after tests.

---

### Tier 4: Cosmetic / Cleanup

#### Fix 7: _unwire_proxy_* always rewrites even when no sentinel found (Comment 2)

**Problem:** `_unwire_proxy_aider` and `_unwire_proxy_hermes_agent` always write the file and return its path, even when `_remove_sentinel_block` returned content unchanged.

**Fix:**
- File: `src/agentalloy/install/subcommands/uninstall_proxy.py`
- Option A: Make `_remove_sentinel_block` return `(new_content, changed: bool)`. Simple but changes the function signature.
- Option B: Compare `content` vs `new_content` before writing. Minimal change.
- I'd go with Option B for now — less invasive. In `_unwire_proxy_aider` and `_unwire_proxy_hermes_agent`, only call `write_text()` and return the path if `new_content != content`.

**Note:** Fix 1 (OpenCode) and Fix 4 (Claude Code) already touch these same functions — coordinate the changes so the "only write if changed" logic applies there too.

---

#### Fix 8: Unused http_request parameter (Comment 6)

**Fix:**
- File: `src/agentalloy/api/proxy_anthropic_router.py`, line 347
- Rename `http_request` to `_http_request` to suppress unused-parameter diagnostics
- Or remove the parameter entirely if FastAPI doesn't require it (it's a `Request` type, not a body parameter — check if removing it breaks anything)

---

#### Fix 9: Missing pyright ignore comments in legacy path (Comment 7)

**Problem:** Legacy wiring calls (lines 507, 513) use `install_state._atomic_write()` without `# pyright: ignore[reportPrivateUsage]`, while proxy calls have them. File header doesn't disable `reportPrivateUsage`.

**Fix:**
- File: `src/agentalloy/install/subcommands/wire_harness.py`
- Add `# pyright: ignore[reportPrivateUsage]` to lines 507 and 513 (the two `_atomic_write` calls in `_wire_legacy`)
- Verify no other `_atomic_write` calls are missing the ignore comment

---

#### Fix 10: Cline docstring says "without overwriting other settings" (Comment 8)

**Fix:**
- File: `src/agentalloy/install/subcommands/wire_harness.py`, function `_wire_proxy_cline` (line 1122)
- Change docstring from: "merges the proxy fields into it without overwriting other settings"
- To: "Overwrites proxy-related keys (`apiProvider`, `apiBaseUrl`, `apiKey`, `model`). Preserves all other keys in the file."

---

## Files That Will Change

| File | Comments addressed |
|------|--------------------|
| `src/agentalloy/install/subcommands/uninstall_proxy.py` | 2, 3, 4 |
| `src/agentalloy/install/subcommands/wire_harness.py` | 1, 7, 8 |
| `src/agentalloy/api/proxy_anthropic_router.py` | 5, 6 |
| `src/agentalloy/api/proxy_router.py` | 9 |
| `tests/install/test_wire_harness.py` | 10 |
| `tests/install/` (new test file or additions) | 1, 3, 4, 5, 9 |

## Execution Order

1. Fix 10 (docstring) — trivial, no test impact
2. Fix 8 (unused parameter) — trivial, no test impact
3. Fix 9 (pyright ignores) — trivial
4. Fix 2 (aider proxy instructions file) — changes wire + uninstall behavior
5. Fix 3 (JSON escaping) — changes proxy error handling
6. Fix 5 (resolve_model fallback) — changes proxy model resolution
7. Fix 7 (uninstall write-if-changed) — refactors uninstall functions
8. Fix 1 (OpenCode uninstall) — depends on Fix 7
9. Fix 4 (Claude Code uninstall) — depends on Fix 7
10. Fix 6 (test Path.home mock) — test infrastructure, last

## Open Questions

- **Comment 5 (streaming error handling):** Should we broaden the error check from `>= 500` to `!= 200`? The non-streaming path already does this. I'd say yes — 401/429 from upstream should propagate to the client, not be silently swallowed.
- **Comment 9 (resolve_model):** Should the error be 400 (bad request) or 503 (service unavailable)? I'd go with 503 — it's the proxy's misconfiguration, not the client's fault.
