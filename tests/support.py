"""Shared test helpers."""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import Any

from agentalloy.lm_client import OpenAICompatClient
from agentalloy.reads.models import ActiveFragment, SkillClass
from agentalloy.storage.protocols import EMBEDDING_DIM


def fake_fragment(
    fid: str,
    ftype: str = "execution",
    *,
    skill: str = "sk-a",
    skill_class: SkillClass = "domain",
    category: str = "design",
) -> ActiveFragment:
    return ActiveFragment(
        fragment_id=fid,
        fragment_type=ftype,
        sequence=1,
        content=f"content of {fid}",
        skill_id=skill,
        version_id=f"{skill}-v1",
        skill_class=skill_class,
        category=category,
        domain_tags=[],
    )


class StubLMClient(OpenAICompatClient):
    """Deterministic stand-in for OpenAICompatClient — no network calls."""

    def __init__(self) -> None:
        pass  # bypass httpx.Client creation

    def list_models(self) -> list[str]:
        return ["stub-embed", "stub-assembly"]

    def ensure_model_loaded(self, model: str) -> None:  # noqa: ARG002
        return None

    def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:  # noqa: ARG002
        out: list[list[float]] = []
        for text in texts:
            h = hashlib.sha256(text.encode()).digest()
            seed = struct.unpack("<Q", h[:8])[0]
            out.append([((seed >> (i % 64)) & 0xFF) / 255.0 for i in range(EMBEDDING_DIM)])
        return out

    def chat(self, **_: Any) -> str:
        return "stub"

    def close(self) -> None:
        pass


def seed_phase(root: Path, phase: str, **fields: str | None) -> None:
    """Put *root* in *phase* in the bound process store.

    The test-side counterpart of ``_write_phase_atomic`` — ``mode``,
    ``free_since`` and ``actor`` ride the same blob. Replaces the
    ``.agentalloy/phase`` file writes the suite used before the store became
    the only source.
    """
    from agentalloy.api.state_router import _repo_key_for  # pyright: ignore[reportPrivateUsage]
    from agentalloy.storage.state_store import process_store

    store = process_store()
    assert store is not None, "no state store bound — is _bound_state_store active?"
    store.for_repo(_repo_key_for(str(root))).write_phase(phase, **fields)  # pyright: ignore[reportArgumentType]


def seed_announced(root: Path, phase: str, keys: list[str] | None = None) -> None:
    """Record ``(phase, keys)`` as already-oriented in the bound process store.

    The test-side counterpart of ``_write_announced_atomic`` — replaces the
    ``.agentalloy/announced`` file writes the suite used before the store became
    the only source. Value shape: ``"<phase>\\t<key1>,<key2>,..."``, or a bare
    ``phase`` when no keys (matches ``_write_announced_atomic``'s encoding).
    """
    from agentalloy.api.state_router import _repo_key_for  # pyright: ignore[reportPrivateUsage]
    from agentalloy.storage.state_store import process_store

    store = process_store()
    assert store is not None, "no state store bound — is _bound_state_store active?"
    ks = [k for k in (keys or []) if k]
    value = f"{phase}\t{','.join(ks)}" if ks else phase
    store.for_repo(_repo_key_for(str(root))).write("announced", value)


def read_announced_raw(root: Path) -> str | None:
    """Read the raw ``announced`` store value (``"<phase>\\t<keys>"`` or bare
    ``phase``), or ``None`` if absent. Test-side counterpart of the file read the
    suite used before the store became the only source."""
    from agentalloy.api.state_router import _repo_key_for  # pyright: ignore[reportPrivateUsage]
    from agentalloy.storage.state_store import process_store

    store = process_store()
    assert store is not None, "no state store bound — is _bound_state_store active?"
    return store.for_repo(_repo_key_for(str(root))).read("announced")


def seed_banner_turns(root: Path, phase: str, session_key: str | None, count: int) -> None:
    """Record the banner carrier-turn counter in the bound process store.

    The test-side counterpart of ``_write_banner_turn_atomic`` — replaces the
    ``.agentalloy/banner-turns`` file writes the suite used before the store
    became the only source.
    """
    from agentalloy.api.state_router import _repo_key_for  # pyright: ignore[reportPrivateUsage]
    from agentalloy.storage.state_store import process_store

    store = process_store()
    assert store is not None, "no state store bound — is _bound_state_store active?"
    value = f"{phase}\t{session_key or ''}\t{count}"
    store.for_repo(_repo_key_for(str(root))).write("banner-turns", value)


def seed_orientation(root: Path, phase: str, keys: list[str] | None = None) -> None:
    """Record ``(phase, keys)`` as already-oriented in the bound process store.

    The test-side counterpart of ``_write_orientation_announced_atomic`` —
    writes the ``orientation`` store row. Value shape:
    ``"<phase>\\t<key1>,<key2>,..."``, or a bare ``phase`` when no keys.
    """
    from agentalloy.api.state_router import _repo_key_for  # pyright: ignore[reportPrivateUsage]
    from agentalloy.storage.state_store import process_store

    store = process_store()
    assert store is not None, "no state store bound — is _bound_state_store active?"
    ks = [k for k in (keys or []) if k]
    value = f"{phase}\t{','.join(ks)}" if ks else phase
    store.for_repo(_repo_key_for(str(root))).write("orientation", value)
