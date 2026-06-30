"""Shared test helpers."""

from __future__ import annotations

import hashlib
import struct
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
