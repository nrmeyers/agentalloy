@Copilot commented on this pull request.

Pull request overview
Copilot reviewed 29 out of 29 changed files in this pull request and generated 10 comments.

In src/agentalloy/install/subcommands/wire_harness.py:

> +    proxy_url = f"http://localhost:{port}/v1"
+    block_lines = [
+        sentinel_begin,
+        f"openai-api-base: {proxy_url}",
+        "openai-api-key: agentalloy",
+        "model: agentalloy-proxy",
+        "read:",
+        "  - .agentalloy-aider-instructions.md",
+        sentinel_end,
In proxy mode, the injected .aider.conf.yml block adds read: - .agentalloy-aider-instructions.md, but proxy wiring does not create that instructions file (and tests assert it should not exist). This leaves aider configured to read a non-existent file. Either create/manage the instructions file in proxy mode, or remove the read entry from the proxy block and adjust uninstall expectations accordingly.

In src/agentalloy/install/subcommands/uninstall_proxy.py:

> +def _unwire_proxy_aider(root: Path) -> list[Path]:
+    """Remove aider proxy config from .aider.conf.yml."""
+    conf_path = root / ".aider.conf.yml"
+    if not conf_path.exists():
+        return []
+    content = conf_path.read_text()
+    # Remove between sentinel comments
+    new_content = _remove_sentinel_block(content)
+    conf_path.write_text(new_content)
+    # Also remove instructions file
+    instr_path = root / ".agentalloy-aider-instructions.md"
+    if instr_path.exists():
+        instr_path.unlink()
+        return [conf_path, instr_path]
+    return [conf_path]
+
+
+def _unwire_proxy_hermes_agent(scope: str, root: Path) -> list[Path]:
+    """Remove hermes-agent proxy config from config.yaml."""
+    config_path = Path.home() / ".hermes" / "config.yaml" if scope == "user" else root / "AGENTS.md"
+    if not config_path.exists():
+        return []
+    content = config_path.read_text()
+    new_content = _remove_sentinel_block(content)
+    config_path.write_text(new_content)
+    return [config_path]
These _unwire_proxy_* functions always rewrite the target file and report it as removed even when no sentinel block is present (because _remove_sentinel_block returns the original content unchanged). This can produce misleading uninstall output and unnecessarily touch user files. Consider detecting whether sentinels were found/removed (e.g., compare old/new content or return a flag) and only write/return the path when a change actually occurred.

In src/agentalloy/install/subcommands/uninstall_proxy.py:

> +def _unwire_proxy_opencode(root: Path) -> list[Path]:
+    """Remove opencode proxy env file."""
+    env_path = root / ".opencode" / ".agentalloy-env"
+    prompt_path = root / ".opencode" / "system-prompt.md"
+    removed: list[Path] = []  # type: ignore[reportUnknownVariableType]
+    if env_path.exists():
+        env_path.unlink()
+        removed.append(env_path)
+    if prompt_path.exists():
+        prompt_path.unlink()
+        removed.append(prompt_path)
+    return removed
Proxy wiring for OpenCode injects a sentinel-bounded block into .opencode/system-prompt.md (preserving existing user content), but uninstall currently unlinks the entire file. This can delete user-managed prompt content. Prefer removing only the sentinel block and only deleting the file if it becomes empty and was fully managed by AgentAlloy.

In src/agentalloy/install/subcommands/uninstall_proxy.py:

> +    if env_path.exists():
+        env_path.unlink()
+        # Print instructions for shell profile cleanup
+        print("Remove the source line from .bashrc/.zshrc manually:", file=sys.stderr)
+        print("  # AgentAlloy: claude-code proxy env", file=sys.stderr)
+        return [env_path]
+    return []
+
+
_wire_proxy_claude_code preserves any existing content in ~/.agentalloy/claude-code-env.sh by replacing/appending a sentinel-bounded block, but uninstall deletes the whole file without checking for the sentinel. This can remove user content or even delete a file not managed by AgentAlloy. Consider removing only the sentinel block and deleting the file only if the remainder is empty (and/or only if sentinels are present).

In src/agentalloy/api/proxy_anthropic_router.py:

> +        async with upstream.stream("POST", "/v1/chat/completions", json=payload) as resp:
+            if resp.status_code >= 500:
+                logger.warning("Upstream streaming returned HTTP %d", resp.status_code)
+                yield (
+                    "event: message_start\n"
+                    f'data: {{"type":"message_start","message":{{"id":"msg_error","type":"message","role":"assistant","content":[],"model":"{model}","stop_reason":null,"stop_sequence":null,"usage":{{"input_tokens":0,"output_tokens":0}}}}}}\n\n'
+                )
+                yield (
+                    "event: error\n"
+                    f'data: {{"type":"error","error":{{"type":"api_error","message":"Upstream returned HTTP {resp.status_code}"}}}}\n\n'
+                )
+                return
The streaming error path builds JSON via f-strings that interpolate model directly into a JSON string. Since model comes from the request and is not escaped, a value containing quotes/newlines can break the JSON/SSE framing (and potentially inject extra fields). Build these data: payloads using json.dumps(...) for all branches. Also consider handling any non-200 upstream status (not just >=500) since upstream may return 401/429/etc in streaming mode.

In src/agentalloy/api/proxy_anthropic_router.py:

> +            "Cache-Control": "no-cache",
+            "Connection": "keep-alive",
+            "X-Accel-Buffering": "no",
+        },
+    )
+
+
+# ---------------------------------------------------------------------------
+# Route handler
+# ---------------------------------------------------------------------------
+
+
+@router.post("/v1/messages", response_model=None)
+async def proxy_anthropic_messages(
+    request: AnthropicRequest,
+    http_request: Request,
This endpoint receives http_request: Request but never uses it. If it’s not needed, remove the parameter; otherwise rename to _http_request to avoid unused-parameter diagnostics under strict type checking.

In src/agentalloy/install/subcommands/wire_harness.py:

>      if dedicated:
-        # We own the entire file
-        install_state._atomic_write(target_path, rendered)  # pyright: ignore[reportPrivateUsage]
+        install_state._atomic_write(target_path, rendered)
         action = "wrote_new_file"
-        # sha256 of the rendered file content for drift detection
         content_sha256 = _sha256(rendered.strip())
     else:
-        # Sentinel-bounded injection
         existing = target_path.read_text() if target_path.exists() else ""
         result_content = _inject_sentinel_block(existing, rendered)
-        install_state._atomic_write(target_path, result_content)  # pyright: ignore[reportPrivateUsage]
+        install_state._atomic_write(target_path, result_content)
         action = "injected_block"
This block now calls install_state._atomic_write(...) without the existing # pyright: ignore[reportPrivateUsage] used elsewhere in this module. With reportPrivateUsage = "warning" enabled, this introduces new pyright warnings/inconsistency. Either restore the ignore comments here or expose a public write helper in install_state and use that consistently.

In src/agentalloy/install/subcommands/wire_harness.py:

> +def _wire_proxy_cline(port: int, root: Path) -> list[dict[str, Any]]:
+    """Wire Cline to use the AgentAlloy proxy.
+
+    Writes ``.cline/settings.json`` with proxy fields (``apiProvider``,
+    ``apiBaseUrl``, ``apiKey``, ``model``).  If the file already exists,
+    merges the proxy fields into it without overwriting other settings.
+    """
+    settings_path = root / ".cline" / "settings.json"
+    settings_path.parent.mkdir(parents=True, exist_ok=True)
+
+    proxy_url = f"http://localhost:{port}/v1"
+    proxy_fields = {
+        "apiProvider": "openai",
+        "apiBaseUrl": proxy_url,
+        "apiKey": "agentalloy",
+        "model": "agentalloy-proxy",
+    }
+
+    if settings_path.exists():
+        try:
+            settings = json.loads(settings_path.read_text())
+        except json.JSONDecodeError:
+            settings = {}
+    else:
+        settings = {}
+
+    settings.update(proxy_fields)
+    serialized = json.dumps(settings, indent=2) + "\n"
Docstring says proxy wiring for Cline "merges ... without overwriting other settings", but settings.update(proxy_fields) will overwrite any existing apiProvider/apiBaseUrl/apiKey/model values. If overwriting those keys is intended (to force proxy usage), clarify the docstring to say it preserves unrelated keys only; if not intended, merge more selectively or store previous values for restoration on uninstall.

In src/agentalloy/api/proxy_router.py:

> +def _resolve_model(model: str, upstream_model: str | None) -> str:
+    """Resolve a model name to the upstream model to forward.
+
+    The synthetic name ``"agentalloy-proxy"`` (used by Continue and other
+    harnesses that point their API base at the proxy) maps to
+    ``upstream_model`` from settings.  Any other name is passed through
+    unchanged, which allows callers that already specify a concrete model
+    (e.g. ``"gpt-4o"``) to work without re-configuration.
+    """
+    if model == "agentalloy-proxy":
+        return upstream_model or model
+    return model
If a client sends model="agentalloy-proxy" and upstream_model is unset/empty, _resolve_model forwards the synthetic name upstream. That will likely produce an opaque upstream error. Consider explicitly treating agentalloy-proxy as requiring a configured upstream model (return a 400/503 with a clear message) rather than passing the synthetic name through.

In tests/install/test_wire_harness.py:

> @@ -372,7 +412,7 @@ def test_records_harness_in_state(self, repo_root: Path) -> None:
     def test_records_files_written(self, repo_root: Path) -> None:
         wire_harness("claude-code", port=8000, root=repo_root)
         st = install_state.load_state(repo_root)
-        assert len(st["harness_files_written"]) == 2  # CLAUDE.md + settings.json hooks
+        assert len(st["harness_files_written"]) == 1  # env file only
These tests call wire_harness("claude-code", ...) in default (proxy) mode without monkeypatching Path.home(). Since proxy wiring writes to ~/.agentalloy/claude-code-env.sh, this can leak artifacts into the developer/CI home directory and make the test suite non-hermetic. Consider patching Path.home in a fixture for this module (or in these specific tests) when exercising claude-code proxy wiring.

—
Reply to this email directly, view it on GitHub, or unsubscribe.
You are receiving this because you authored the thread.

