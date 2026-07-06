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


CASES: tuple[HarnessCase, ...] = (
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
        name="codex",
        binary="codex",
        env=_openai_proj_env,
        argv=lambda root: ["codex", "exec", "--skip-git-repo-check", PROMPT],
        notes="Env-only (wrap path): user-scoped ~/.codex/config.toml is never touched.",
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
        env=_openai_proj_env,
        argv=lambda root: ["openclaw", "run", PROMPT],
        notes="Env-only (wrap path): user-scoped ~/.openclaw/plugins.json is never touched.",
    ),
)
