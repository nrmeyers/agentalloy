"""Per-harness drivers for the e2e matrix.

Each driver says how to point one real harness binary at the proxy and run a
single headless prompt. Repo-scoped carriers are written with the SAME
functions ``agentalloy wire`` uses (so the matrix exercises our real wiring);
harnesses whose persistent carrier is user-scoped (codex, openclaw,
claude-code's env.sh fallback) are driven through their ``env_builder``
vector instead — equivalent to the ``agentalloy wrap`` launch path — so the
matrix never mutates the developer's real home directory.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from agentalloy.api.proxy_context import encode_proj_token

PROMPT = "Reply with the single word READY and nothing else."


@dataclass(frozen=True)
class HarnessCase:
    """One matrix entry: how to wire, launch, and bound one harness binary."""

    name: str
    binary: str
    # Returns extra env for the child process.
    env: Callable[[int, Path], dict[str, str]]
    # Returns the argv to run headlessly in the work repo.
    argv: Callable[[Path], list[str]]
    # Writes the repo-scoped carrier via the real wiring (None = env-only).
    wire: Callable[[int, Path], object] | None = None
    timeout: int = 180
    notes: str = ""
    # Env vars that must be scrubbed so the dev machine's own wiring can't leak in.
    scrub_env: tuple[str, ...] = field(default=())
    # Known-broken with a tracked cause: the matrix xfails instead of failing.
    xfail_reason: str = ""


def _wire_aider(port: int, root: Path) -> object:
    from agentalloy.install.subcommands.wire_harness import (
        _wire_proxy_aider,  # pyright: ignore[reportPrivateUsage]
    )

    return _wire_proxy_aider(port, root)


def _wire_opencode(port: int, root: Path) -> object:
    from agentalloy.install.subcommands.wire_harness import (
        _wire_proxy_opencode,  # pyright: ignore[reportPrivateUsage]
    )

    return _wire_proxy_opencode(port, root)


def _wire_copilot(port: int, root: Path) -> object:
    from agentalloy.providers.copilot_cli import install as copilot_install

    return copilot_install.apply_persistent_config(port, root)


def _anthropic_env(port: int, root: Path) -> dict[str, str]:
    token = encode_proj_token(root)
    return {"ANTHROPIC_BASE_URL": f"http://localhost:{port}/proj/{token}"}


def _openai_proj_env(port: int, root: Path) -> dict[str, str]:
    token = encode_proj_token(root)
    return {
        "OPENAI_BASE_URL": f"http://localhost:{port}/proj/{token}/v1",
        "OPENAI_API_KEY": "agentalloy",
    }


def _copilot_env(port: int, root: Path) -> dict[str, str]:
    from agentalloy.providers.copilot_cli.install import build_env

    return build_env(port, root)


def _wire_hermes(port: int, root: Path) -> object:
    from agentalloy.install.subcommands import wire_harness

    # Sandbox rule: the matrix must never restart the developer's live hermes
    # gateway. The one-shot `hermes -z` CLI path talks to the model endpoint
    # directly, so the repo-local config + HERMES_HOME env suffice.
    orig = wire_harness._restart_hermes_gateway  # pyright: ignore[reportPrivateUsage]
    wire_harness._restart_hermes_gateway = lambda root: True
    try:
        return wire_harness._wire_proxy_hermes_agent(  # pyright: ignore[reportPrivateUsage]
            port, root, scope="repo"
        )
    finally:
        wire_harness._restart_hermes_gateway = orig


def _hermes_env(port: int, root: Path) -> dict[str, str]:
    # Mirrors .hermes/.agentalloy-env; dummy key only ever reaches the stub.
    return {"HERMES_HOME": str(root / ".hermes"), "OPENAI_API_KEY": "agentalloy-e2e"}


def _wire_openclaw(port: int, root: Path) -> object:
    # Sandbox rule: never touch the developer's real ~/.openclaw. Render the
    # same config the real wiring merges, but into a work-repo state dir that
    # OPENCLAW_STATE_DIR/OPENCLAW_CONFIG_PATH point at.
    import json

    from agentalloy.providers.openclaw.install import render_config

    state = root / ".openclaw-state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "openclaw.json").write_text(json.dumps(render_config(port), indent=2))
    return None


def _openclaw_env(port: int, root: Path) -> dict[str, str]:
    state = root / ".openclaw-state"
    return {
        "OPENCLAW_STATE_DIR": str(state),
        "OPENCLAW_CONFIG_PATH": str(state / "openclaw.json"),
    }


def _wire_cline(port: int, root: Path) -> object:
    # Sandbox rule: never touch the developer's real ~/.cline. Render the same
    # providers.json the real wiring merges, into a --data-dir sandbox.
    import json

    from agentalloy.providers.cline.install import providers_json_path, render_providers

    data_dir = root / ".cline-data"
    path = providers_json_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(render_providers(port), indent=2))
    return None


def _wire_codex(port: int, root: Path) -> object:
    from agentalloy.providers.codex import install as codex_install

    return codex_install.apply_persistent_config(port, root)


def _codex_env(port: int, root: Path) -> dict[str, str]:
    # Mirrors .codex/.agentalloy-env; the key is a dummy — it only ever
    # reaches the stub upstream (auth-transparent forward).
    return {"CODEX_HOME": str(root / ".codex"), "OPENAI_API_KEY": "agentalloy-e2e"}


def _wire_continue(port: int, root: Path) -> object:
    from agentalloy.install.subcommands.wire_harness import (
        _wire_proxy_continue,  # pyright: ignore[reportPrivateUsage]
    )

    return _wire_proxy_continue("continue-local", port, root)


CASES: tuple[HarnessCase, ...] = (
    HarnessCase(
        name="continue-local",
        binary="cn",
        env=lambda port, root: {},
        argv=lambda root: [
            "cn",
            # The cn CLI does not auto-discover repo agents; the IDE extension
            # does. Pointing --config at the exact carrier our wiring wrote
            # still verifies the carrier end to end.
            "--config",
            str(root / ".continue" / "agents" / "agentalloy.yaml"),
            "-p",
            PROMPT,
        ],
        wire=_wire_continue,
        scrub_env=("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE"),
        notes="Modern .continue/agents/agentalloy.yaml carrier (per-repo /proj/<token>).",
    ),
    HarnessCase(
        name="claude-code",
        binary="claude",
        env=_anthropic_env,
        argv=lambda root: ["claude", "-p", PROMPT, "--max-turns", "1"],
        notes=(
            "Native Anthropic passthrough via /proj/<token>; auth-transparent — "
            "any inherited credential only ever reaches the local stub."
        ),
    ),
    HarnessCase(
        name="opencode",
        binary="opencode",
        env=lambda port, root: {},
        argv=lambda root: ["opencode", "run", PROMPT],
        wire=_wire_opencode,
        scrub_env=("OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_API_KEY"),
        timeout=300,  # first run fetches @ai-sdk/openai-compatible from npm
        notes="Repo-local opencode.json provider block (per-repo /proj/<token>).",
    ),
    HarnessCase(
        name="aider",
        binary="aider",
        env=lambda port, root: {"OPENAI_API_KEY": "agentalloy"},
        argv=lambda root: [
            "aider",
            "--message",
            PROMPT,
            "--yes-always",
            "--no-git",
            "--no-check-update",
            "--no-analytics",
        ],
        wire=_wire_aider,
        scrub_env=("OPENAI_API_BASE", "OPENAI_BASE_URL"),
        notes="Reads .aider.conf.yml from cwd — exercises the sentinel YAML carrier.",
    ),
    HarnessCase(
        name="cline",
        binary="cline",
        env=lambda port, root: {},
        argv=lambda root: [
            "cline",
            "--data-dir",
            str(root / ".cline-data"),
            "--auto-approve",
            "false",
            PROMPT,
        ],
        wire=_wire_cline,
        notes="Sandboxed --data-dir with the rendered providers.json store (bare /v1).",
    ),
    HarnessCase(
        name="hermes-agent",
        binary="hermes",
        env=_hermes_env,
        argv=lambda root: ["hermes", "-z", PROMPT, "--cli"],
        wire=_wire_hermes,
        notes=(
            "Repo-local HERMES_HOME (config.yaml model block → /proj/<token>/v1); "
            "gateway restart stubbed — one-shot CLI path needs no gateway."
        ),
    ),
    HarnessCase(
        name="codex",
        binary="codex",
        env=_codex_env,
        argv=lambda root: ["codex", "exec", "--skip-git-repo-check", PROMPT],
        wire=_wire_codex,
        scrub_env=("OPENAI_BASE_URL",),
        notes=(
            "Repo-local CODEX_HOME (config.toml, wire_api=responses) → the "
            "proxy's native /proj/<token>/v1/responses passthrough."
        ),
    ),
    HarnessCase(
        name="copilot-cli",
        binary="copilot",
        env=_copilot_env,
        argv=lambda root: ["copilot", "-p", PROMPT, "--allow-all-tools"],
        wire=_wire_copilot,
        notes="BYOK env vars (COPILOT_PROVIDER_*); carrier written for parity.",
    ),
    HarnessCase(
        name="openclaw",
        binary="openclaw",
        env=_openclaw_env,
        argv=lambda root: [
            "openclaw",
            "agent",
            "--local",
            "-m",
            PROMPT,
            "--json",
            "--session-id",
            "agentalloy-e2e",
        ],
        wire=_wire_openclaw,
        notes=(
            "Sandboxed OPENCLAW_STATE_DIR with the rendered custom-provider "
            "config; user-scoped assistant → bare /v1 surface."
        ),
    ),
)
