"""CLI entry point for AgentAlloy."""

import argparse
import sys
from os import environ
from pathlib import Path
from typing import Any

from agentalloy.config import Config


def _local_index_build(repo_path: Path, config: Config) -> None:
    """Offline fallback for `add`: build a standalone index under the repo.

    Only used when the service is unreachable — the service's shared index
    (POST /reindex) is the one MCP tools actually query.
    """
    from agentalloy.code_index.embed_client import EmbedClient
    from agentalloy.code_index.fts import FtsIndex
    from agentalloy.code_index.open import fts_dir, open_codegraph
    from agentalloy.code_index.pipeline import ingest_all_repos

    try:
        index_dir = str(repo_path / ".agentalloy" / "index")
        store = open_codegraph(index_dir)
        embed = EmbedClient(config.embed_url, model=config.embed_model)
        reports = ingest_all_repos(
            store,
            [(repo_path.name, repo_path)],
            embed_client=embed,
            index_dir=index_dir,
        )
        symbols = sum(r.symbols for r in reports)
        chunks = FtsIndex(fts_dir(index_dir)).count() if reports else 0
        print(f"  Indexed: {symbols} symbols, {chunks} chunks")
    except Exception as e:
        print(f"  Index skipped: {e}")


def main() -> int:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="AgentAlloy — local agent with MiniCPM5 interpreter + LangGraph workflow"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # serve command — REST API server
    serve_parser = subparsers.add_parser("serve", help="Start the REST API server")
    serve_parser.add_argument(
        "--host",
        default=environ.get("AGENTALLOY_HOST", "127.0.0.1"),
        help="Host to bind to (default: AGENTALLOY_HOST or 127.0.0.1; endpoints are unauthenticated)",
    )
    serve_parser.add_argument("--port", type=int, default=None, help="Port (default: from config)")

    # proxy command — steering proxy
    proxy_parser = subparsers.add_parser("proxy", help="Start the token-accurate steering proxy")
    proxy_parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    proxy_parser.add_argument("--port", type=int, default=None, help="Port (default: from config)")

    # mcp command — MCP server
    mcp_parser = subparsers.add_parser(
        "mcp", help="Start the MCP server (tool provider for harnesses)"
    )
    mcp_parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport protocol",
    )
    mcp_parser.add_argument("--port", type=int, default=8765, help="Port for SSE transport")

    # status command
    subparsers.add_parser("status", help="Show current status")

    # ask command — interpreter CLI
    ask_parser = subparsers.add_parser("ask", help="Ask the interpreter a question")
    ask_parser.add_argument("question", help="Question to ask")

    # index command — build the code index
    index_parser = subparsers.add_parser(
        "index", help="Build the code index (tree-sitter + tantivy + dense)"
    )
    index_parser.add_argument("--repo", default=".", help="Repository root to index")
    index_parser.add_argument("--embed-url", default=None, help="Embedding server URL (optional)")

    # harness command — configure harness integration
    harness_parser = subparsers.add_parser(
        "harness", help="Configure harness integration (Qwen Code, Cursor, MCP)"
    )
    harness_parser.add_argument(
        "action",
        choices=["setup", "status"],
        help="Action to perform",
    )
    harness_parser.add_argument(
        "--type",
        choices=["qwen-code", "cursor", "generic-mcp"],
        default="qwen-code",
        help="Harness type (for setup)",
    )
    harness_parser.add_argument(
        "--mode",
        choices=["proxy", "mcp", "dual"],
        default="dual",
        help="Integration mode (for setup)",
    )
    harness_parser.add_argument(
        "--project-dir",
        default=".",
        help="Project directory for config files",
    )
    harness_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be written without writing",
    )

    # sessions command — manage workflow sessions
    sessions_parser = subparsers.add_parser(
        "sessions", help="Manage workflow sessions (stash/resume/archive)"
    )
    sessions_parser.add_argument(
        "action",
        choices=["list", "create", "stash", "resume", "archive", "cancel", "status"],
        help="Session action",
    )
    sessions_parser.add_argument("key", nargs="?", default="default", help="Session key")

    # add command — wire a repo to the running service (mirrors v1's `agentalloy add`)
    add_parser = subparsers.add_parser(
        "add", help="Add this repo to the running AgentAlloy service"
    )
    add_parser.add_argument(
        "harness",
        nargs="?",
        choices=["qwen-code", "cursor", "generic-mcp"],
        default="qwen-code",
        help="Harness type to wire (default: qwen-code)",
    )
    add_parser.add_argument(
        "--repo",
        default=".",
        help="Repo path (default: current directory)",
    )
    add_parser.add_argument(
        "--mode",
        choices=["proxy", "mcp", "dual"],
        default="dual",
        help="Integration mode (default: dual)",
    )
    add_parser.add_argument(
        "--mcp-tools",
        choices=["slim", "full", "none"],
        default="slim",
        help=(
            "MCP tool surface for the MAIN model. slim (default) = "
            "code_search + contract_detail (~230 schema tokens/turn); "
            "full = all 13 tools (~1.5k tokens/turn); none = no MCP server "
            "(steering brief only). State work always runs on the sidecar."
        ),
    )

    # upstream command — show or change the proxy's upstream LLM
    upstream_parser = subparsers.add_parser(
        "upstream", help="Show or change the proxy's upstream LLM (URL + model)"
    )
    upstream_parser.add_argument(
        "action",
        nargs="?",
        choices=["get", "set"],
        default="get",
        help="Action (default: get)",
    )
    upstream_parser.add_argument(
        "url",
        nargs="?",
        help="Upstream base URL (set)",
    )
    upstream_parser.add_argument(
        "--model",
        default="",
        help="Model name served by the upstream (set)",
    )
    upstream_parser.add_argument(
        "--key",
        default="",
        help="API key for the upstream (set; keeps current key if omitted)",
    )

    args = parser.parse_args()

    if args.command == "serve":
        from agentalloy.server import start_server

        config = Config.from_env()
        port = args.port or config.service_port
        print(f"Starting AgentAlloy server on {args.host}:{port}")
        start_server(host=args.host, port=port)
        return 0

    elif args.command == "proxy":
        import uvicorn

        from agentalloy.proxy import proxy_app

        config = Config.from_env()
        port = args.port or config.proxy_port
        print(f"Starting steering proxy on {args.host}:{port}")
        print(f"  Upstream: {config.upstream_url}")
        uvicorn.run(proxy_app, host=args.host, port=port)
        return 0

    elif args.command == "mcp":
        import sys

        from agentalloy.mcp_server import _SERVICE_URL, run

        # stderr only — stdout is the JSON-RPC channel on the stdio transport
        print(
            f"Starting MCP bridge → {_SERVICE_URL} (transport={args.transport})",
            file=sys.stderr,
        )
        run(args.transport, args.port)
        return 0

    elif args.command == "status":
        config = Config.from_env()
        print("AgentAlloy v2.0")
        print(f"  Service port:  {config.service_port}")
        print(f"  Proxy port:    {config.proxy_port}")
        print(f"  Model port:    {config.model_port}")
        print(f"  Embed port:    {config.embed_port}")
        print(f"  Upstream:      {config.upstream_url}")
        print(f"  State DB:      {config.state_duck}")
        print(f"  Index dir:     {config.index_dir}")
        return 0

    elif args.command == "ask":
        from openai import OpenAI

        from agentalloy.code_index.embed_client import EmbedClient
        from agentalloy.code_index.fts import FtsIndex
        from agentalloy.code_index.open import fts_dir, open_codegraph
        from agentalloy.code_index.pipeline import ingest_all_repos
        from agentalloy.code_index.retrieval.hybrid import CodeSearcher
        from agentalloy.executors import set_graph_index, set_store
        from agentalloy.interpreter import Interpreter
        from agentalloy.state_store import StateStore

        config = Config.from_env()
        store = StateStore(config.state_duck)
        set_store(store)

        # Wire the code graph index (lexical-only when no embed server).
        try:
            gstore = open_codegraph(config.index_dir)
            embed = EmbedClient(config.embed_url, model=config.embed_model)
            ingest_all_repos(
                gstore,
                [(Path(config.repo_root).resolve().name, Path(config.repo_root))],
                embed_client=embed,
                index_dir=config.index_dir,
            )
            fts = FtsIndex(fts_dir(config.index_dir))
            set_graph_index(gstore, CodeSearcher(gstore, fts, embed))
        except Exception:
            pass

        client = OpenAI(
            base_url=f"http://localhost:{config.model_port}/v1",
            api_key=config.model_key,
        )
        interp = Interpreter(
            client=client,
            max_steps=config.max_steps,
            hard_cap=config.hard_cap,
            state_store=store,
        )

        messages = [{"role": "user", "content": args.question}]
        result = interp.run(messages)

        print(f"\nStop reason: {result.stop_reason}")
        print(f"Steps: {result.steps}")
        print(f"Tool calls: {len(result.tool_calls)}")
        if result.answer:
            print(f"\nAnswer: {result.answer}")

        store.close()
        return 0

    elif args.command == "index":
        from agentalloy.code_index.embed_client import EmbedClient
        from agentalloy.code_index.open import open_codegraph
        from agentalloy.code_index.pipeline import ingest_all_repos

        config = Config.from_env()
        embed_url = args.embed_url or config.embed_url
        repo = Path(args.repo).resolve()
        print(f"Building index: repo={repo}, embed={embed_url or 'none'}")

        store = open_codegraph(config.index_dir)
        reports = ingest_all_repos(
            store,
            [(repo.name, repo)],
            embed_client=EmbedClient(embed_url, model=config.embed_model),
            index_dir=config.index_dir,
            force_full=True,
        )
        for r in reports:
            mode = "hybrid" if r.embed_available else "lexical-only"
            print(
                f"{r.repo}: {r.symbols} symbols, {r.edges} edges, "
                f"{r.embedded} embedded, mode={mode}"
            )
        return 0

    elif args.command == "harness":
        from agentalloy.harness import (
            generate_harness_config,
            harness_status,
            write_harness_config,
        )

        config = Config.from_env()

        if args.action == "setup":
            harness_config = generate_harness_config(
                harness_type=args.type,
                mode=args.mode,
                service_port=config.service_port,
                proxy_port=config.proxy_port,
                project_dir=args.project_dir,
            )
            written = write_harness_config(harness_config, dry_run=args.dry_run)

            print(f"Harness: {args.type} ({args.mode} mode)")
            print()
            if written:
                print("Configuration files:")
                for path in written:
                    print(f"  {path}")
                print()
            if harness_config.instructions:
                print("Setup instructions:")
                for instr in harness_config.instructions:
                    print(f"  {instr}")

        elif args.action == "status":
            status = harness_status(config.service_port)
            print(f"Service: {status['service']}")
            print(f"Phase: {status['phase']}")
            print(f"Skills: {status['skills']}")
            if status["gates"]:
                print("Gates:")
                for gate in status["gates"]:
                    phase = gate.get("phase", "?")
                    has_artifact = gate.get("has_exit_artifact", False)
                    approved = gate.get("approved", None)
                    status_str = "✓" if approved else ("~" if has_artifact else "✗")
                    print(f"  {status_str} {phase}")

        return 0

    elif args.command == "sessions":
        from agentalloy.sessions import SessionManager
        from agentalloy.state_store import StateStore

        config = Config.from_env()
        store = StateStore(config.state_duck)
        mgr = SessionManager(store)

        if args.action == "list":
            sessions = mgr.list_sessions()
            if not sessions:
                print("No sessions found.")
            else:
                print(f"{'Key':<20} {'Status':<12} {'Phase':<10} {'Updated'}")
                print("-" * 60)
                for s in sessions:
                    updated = s.updated_at or "—"
                    print(f"{s.session_key:<20} {s.status:<12} {s.phase:<10} {updated}")

        elif args.action == "create":
            info = mgr.create(args.key)
            print(f"Created session: {info.session_key} (phase={info.phase})")

        elif args.action == "stash":
            snapshot = mgr.stash(args.key)
            if snapshot:
                print(f"Stashed session: {args.key} (phase={snapshot.get('phase', '?')})")
            else:
                print(f"Session '{args.key}' not found or not active.")

        elif args.action == "resume":
            resumed = mgr.resume(args.key)
            if resumed:
                print(f"Resumed session: {args.key} (phase={resumed.get('phase', '?')})")
            else:
                print(f"Session '{args.key}' not found or not stashed.")

        elif args.action == "archive":
            ok = mgr.archive(args.key)
            print(f"{'Archived' if ok else 'Not found'}: {args.key}")

        elif args.action == "cancel":
            ok = mgr.cancel(args.key)
            print(f"{'Cancelled' if ok else 'Not found'}: {args.key}")

        elif args.action == "status":
            status = mgr.status(args.key)
            if "error" in status:
                print(f"Error: {status['error']}")
            else:
                print(f"Session: {status['session_key']}")
                print(f"  Status: {status['status']}")
                print(f"  Phase:  {status['phase']}")
                if status.get("work_items"):
                    print(f"  Work items ({len(status['work_items'])}):")
                    for item in status["work_items"]:
                        print(f"    [{item['status']}] {item['task_slug']}")

        store.close()
        return 0

    elif args.command == "add":
        import json
        import subprocess
        import urllib.request

        repo_path = Path(args.repo).resolve()
        if not repo_path.is_dir():
            print(f"Error: {repo_path} is not a directory")
            return 1

        config = Config.from_env()
        print(f"Adding repo: {repo_path}")
        print(f"Harness: {args.harness} ({args.mode} mode)")
        print()

        # 1. Ensure service is running
        print("Checking service...")
        try:
            import urllib.request

            req = urllib.request.Request(
                f"http://localhost:{config.service_port}/health", method="GET"
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    print(f"  Service running on :{config.service_port}")
        except Exception:
            print("  Service not running — starting in background...")
            subprocess.Popen(
                ["agentalloy", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            # Wait for it to come up
            import time

            for _ in range(15):
                time.sleep(1)
                try:
                    req = urllib.request.Request(
                        f"http://localhost:{config.service_port}/health", method="GET"
                    )
                    with urllib.request.urlopen(req, timeout=2):
                        print(f"  Service started on :{config.service_port}")
                        break
                except Exception:
                    continue
            else:
                print("  Warning: service didn't start — start manually with `agentalloy serve`")

        # 2. Index the repo into the service's shared multi-repo index.
        # The service's index is the one MCP code_search reads, so it is the
        # authoritative one. Local build is only the offline fallback.
        print("Indexing repo...")
        service_up = False
        try:
            import urllib.request

            health_req = urllib.request.Request(
                f"http://localhost:{config.service_port}/health", method="GET"
            )
            urllib.request.urlopen(health_req, timeout=3).close()
            service_up = True
        except Exception:
            pass

        if service_up:
            import urllib.request

            payload = json.dumps({"repo": str(repo_path)}).encode()
            req = urllib.request.Request(
                f"http://localhost:{config.service_port}/reindex",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=600) as resp:
                    reindex_info = json.loads(resp.read())
            except Exception as e:
                reindex_info = {"status": "error", "message": str(e)}
            if reindex_info.get("status") == "ok":
                print(
                    f"  Indexed into service: {reindex_info.get('symbols', 0)} symbols, "
                    f"{reindex_info.get('chunks', 0)} chunks "
                    f"({len(reindex_info.get('repos', []))} repo(s) registered)"
                )
            else:
                print(f"  Reindex failed: {reindex_info.get('message', reindex_info)}")
        else:
            print("  Service offline — building local index instead")
            _local_index_build(repo_path, config)

        # 3. Write harness config (all-in-one — no manual steps)
        print("Configuring harness...")

        if args.harness == "qwen-code":
            qwen_dir = repo_path / ".qwen"
            qwen_dir.mkdir(parents=True, exist_ok=True)
            settings_path = qwen_dir / "settings.json"

            # Start from the user's global settings (preserves all their config)
            global_settings = Path.home() / ".qwen" / "settings.json"
            settings: dict[str, Any] = {}
            if global_settings.exists():
                try:
                    settings = json.loads(global_settings.read_text())
                except Exception:
                    settings = {}

            # Project scope: phase/contracts for this repo live under its
            # own key — the proxy reads it from the baseUrl prefix, the MCP
            # server from its env. Same derivation everywhere.
            from agentalloy.registry import project_key

            proj = project_key(repo_path)

            # Add MCP server config. The tool surface defaults to slim —
            # every registered schema is resent to the main model each
            # turn, so the full 13-tool set (~1.5k tokens/turn) is opt-in.
            mcp_tools = getattr(args, "mcp_tools", "slim")
            if mcp_tools == "none":
                if "mcpServers" in settings:
                    settings["mcpServers"].pop("agentalloy", None)
            else:
                if "mcpServers" not in settings:
                    settings["mcpServers"] = {}
                settings["mcpServers"]["agentalloy"] = {
                    "command": "agentalloy",
                    "args": ["mcp", "--transport", "stdio"],
                    "env": {
                        "AGENTALLOY_SERVICE_PORT": str(config.service_port),
                        "AGENTALLOY_PROJECT": proj,
                        "AGENTALLOY_MCP_TOOLS": mcp_tools,
                    },
                    # Without trust the server sits at "Pending approval"
                    # and its tools never register in headless sessions —
                    # the MCP path was silently dead in every scripted run.
                    "trust": True,
                }
                # Belt and braces: qwen's allowlist (settings key
                # mcp.allowed, legacy allowMCPServers /
                # --allowed-mcp-server-names) must name the server or
                # headless runs leave it pending approval.
                mcp_settings = settings.setdefault("mcp", {})
                allowed = mcp_settings.setdefault("allowed", [])
                if "agentalloy" not in allowed:
                    allowed.append("agentalloy")

            # Register the proxy as a model provider + select it
            if args.mode in ("proxy", "dual"):
                proxy_url = f"http://localhost:{config.proxy_port}/p/{proj}/v1"

                # Add proxy as a provider entry
                providers = settings.setdefault("modelProviders", {})
                openai_providers = providers.setdefault("openai", [])
                # Remove any existing agentalloy provider entry
                openai_providers = [
                    p for p in openai_providers if p.get("id") != "agentalloy-proxy"
                ]
                openai_providers.insert(
                    0,
                    {
                        "id": "agentalloy-proxy",
                        "name": "AgentAlloy (steering proxy)",
                        "baseUrl": proxy_url,
                        "envKey": "OPENAI_API_KEY",
                        "generationConfig": {
                            "contextWindowSize": 131072,
                        },
                    },
                )
                providers["openai"] = openai_providers

                # Select the proxy as the active model
                settings["model"] = {
                    "name": "agentalloy-proxy",
                    "baseUrl": proxy_url,
                }
                settings["defaultModel"] = {
                    "authType": "openai",
                    "modelId": "agentalloy-proxy",
                }

            settings_path.write_text(json.dumps(settings, indent=2))
            print(f"  Wrote {settings_path} (merged from global settings)")

            # qwen binds MCP approval to the server's exact config hash and
            # holds unapproved servers at "Pending approval" — where their
            # tools silently never register (headless runs can't click
            # through the prompt). Approve right after every settings write
            # so the just-written config is the one bound.
            if mcp_tools != "none":
                import shutil as _shutil

                qwen_bin = _shutil.which("qwen")
                if qwen_bin:
                    try:
                        result = subprocess.run(
                            [qwen_bin, "mcp", "approve", "agentalloy"],
                            cwd=str(repo_path),
                            capture_output=True,
                            text=True,
                            timeout=60,
                        )
                        if result.returncode == 0:
                            print("  Approved agentalloy MCP server")
                        else:
                            print(
                                "  Warning: MCP approval failed — run "
                                "`qwen mcp approve agentalloy` in the repo: "
                                f"{(result.stderr or result.stdout).strip()[:200]}"
                            )
                    except (OSError, subprocess.TimeoutExpired) as e:
                        print(f"  Warning: MCP approval failed: {e}")
                else:
                    print(
                        "  Note: qwen not on PATH — approve the MCP server "
                        "with `qwen mcp approve agentalloy` before headless use"
                    )

            # 4. Start proxy if dual mode
            if args.mode in ("proxy", "dual"):
                print("Starting steering proxy...")
                # Check if proxy is already running
                proxy_running = False
                try:
                    import socket as sock_mod

                    sock = sock_mod.socket(sock_mod.AF_INET, sock_mod.SOCK_STREAM)
                    sock.settimeout(1)
                    sock.connect(("127.0.0.1", config.proxy_port))
                    sock.close()
                    proxy_running = True
                except Exception:
                    pass

                if proxy_running:
                    print(f"  Proxy already running on :{config.proxy_port}")
                else:
                    subprocess.Popen(
                        ["agentalloy", "proxy"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    import time

                    time.sleep(2)
                    print(f"  Proxy started on :{config.proxy_port}")

        elif args.harness == "cursor":
            cursor_dir = repo_path / ".cursor"
            rules_dir = cursor_dir / "rules"
            rules_dir.mkdir(parents=True, exist_ok=True)

            # Write MCP config
            mcp_path = cursor_dir / "mcp.json"
            mcp_config = {
                "mcpServers": {
                    "agentalloy": {
                        "command": "agentalloy",
                        "args": ["mcp", "--transport", "stdio"],
                    }
                }
            }
            mcp_path.write_text(json.dumps(mcp_config, indent=2))
            print(f"  Wrote {mcp_path}")

            # Write rules file
            from agentalloy.harness import _build_cursor_rules

            rules_path = rules_dir / "agentalloy.mdc"
            rules_path.write_text(_build_cursor_rules(config.service_port, config.proxy_port))
            print(f"  Wrote {rules_path}")

        else:
            # generic-mcp
            from agentalloy.harness import generate_harness_config, write_harness_config

            harness_config = generate_harness_config(
                harness_type=args.harness,
                mode=args.mode,
                service_port=config.service_port,
                proxy_port=config.proxy_port,
                project_dir=str(repo_path),
            )
            written = write_harness_config(harness_config)
            for path in written:
                print(f"  Wrote {path}")

        print()
        print("Done. Start a new session — AgentAlloy is wired in.")
        return 0

    elif args.command == "upstream":
        config = Config.from_env()
        proxy_url = f"http://127.0.0.1:{config.proxy_port}"

        import json
        import urllib.error
        import urllib.request

        if args.action == "set":
            if not args.url or not args.model:
                print("usage: agentalloy upstream set URL --model MODEL [--key KEY]")
                return 1

            payload = json.dumps({"url": args.url, "model": args.model, "key": args.key}).encode()
            req = urllib.request.Request(
                f"{proxy_url}/upstream",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    upstream_info = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                # The proxy validated the new upstream and rejected it.
                try:
                    upstream_info = json.loads(e.read())
                except (ValueError, OSError):
                    upstream_info = {"status": "error", "message": str(e)}
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                # Proxy down: persist to env.sh, effective on next start.
                from agentalloy.instance_env import instance_env_path, update_env_vars

                updates = {"AGENTALLOY_UPSTREAM_URL": args.url, "AGENTALLOY_MODEL": args.model}
                if args.key:
                    updates["AGENTALLOY_UPSTREAM_KEY"] = args.key
                if update_env_vars(instance_env_path(config.state_duck), updates):
                    print(f"  Proxy not running — persisted to env.sh ({args.url}, {args.model})")
                    print("  Takes effect on the next proxy start.")
                else:
                    print(
                        "  Proxy not running and no env.sh to persist to "
                        f"(expected next to {config.state_duck})."
                    )
                return 0

            if upstream_info.get("status") == "ok":
                print(
                    f"  Upstream now: {upstream_info.get('upstream_url')} "
                    f"({upstream_info.get('model')})"
                )
                if not upstream_info.get("persisted"):
                    print("  Note: change is live, but no env.sh was found to persist it")
                return 0
            print(f"  Upstream change rejected: {upstream_info.get('message', upstream_info)}")
            available = upstream_info.get("available")
            if available:
                print(f"  Available: {', '.join(available)}")
            return 1

        # action == "get"
        try:
            with urllib.request.urlopen(f"{proxy_url}/upstream", timeout=3) as resp:
                upstream_info = json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
            from agentalloy.instance_env import instance_env_path, read_env_vars

            env_vars = read_env_vars(instance_env_path(config.state_duck))
            if not env_vars:
                print(f"  Proxy not reachable at {proxy_url} and no env.sh found")
                print(f"  Build-time defaults: {config.upstream_url} ({config.model})")
                return 1
            print("  (proxy not running — values from env.sh)")
            print(f"  Upstream: {env_vars.get('AGENTALLOY_UPSTREAM_URL', config.upstream_url)}")
            print(f"  Model:    {env_vars.get('AGENTALLOY_MODEL', config.model)}")
            return 0
        print(f"  Upstream: {upstream_info.get('upstream_url')}")
        print(f"  Model:    {upstream_info.get('model')}")
        return 0

    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
