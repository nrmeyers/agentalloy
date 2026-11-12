"""Tests for the reembed CLI's idempotency fast path (corpus content stamp).

A successful full-scope pass stamps ``corpus_meta.last_reembed`` with the
corpus content hash plus the model and card-index mode the vectors were
built with. A later full-scope run whose stamp still matches exits before
taking the writer lock — the observable contract under test is that the
normal path (discovery, embedding, service stop) is never entered.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from typing import Any

import pytest

from agentalloy.config import Settings
from agentalloy.reembed import cli as reembed_cli
from agentalloy.storage.card_index import META_KEY_LAST_REEMBED
from agentalloy.storage.open import open_skills
from agentalloy.storage.protocols import EMBEDDING_DIM, FragmentRow, SkillRow, SkillVersionRow

MODEL_A = "fake-embed-v1"
MODEL_B = "fake-embed-v2"


def _vec_for(text: str) -> list[float]:
    """Deterministic pseudo-embedding: text-seeded RNG → unit vector."""
    rng = random.Random(text)
    vec = [rng.gauss(0.0, 1.0) for _ in range(EMBEDDING_DIM)]
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


class FakeEmbedClient:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [_vec_for(t) for t in texts]

    def close(self) -> None:
        pass


def _seed_skill(
    store: Any,
    skill_id: str,
    name: str,
    tag: str,
    *,
    fragments: int = 2,
    description: str | None = None,
) -> str:
    """Seed a skill with an active version and ``fragments`` fragments."""
    version_id = f"{skill_id}::v1"
    store.insert_version(
        SkillVersionRow(
            version_id=version_id,
            skill_id=skill_id,
            version_number=1,
            authored_at=datetime.now(UTC),
            author="test",
            change_summary="initial",
            status="active",
            raw_prose=f"raw prose for {name}",
        )
    )
    store.insert_skill(
        SkillRow(
            skill_id=skill_id,
            canonical_name=name,
            category="testing",
            skill_class="domain",
            domain_tags=[tag],
            deprecated=False,
            superseded_by=None,
            always_apply=False,
            phase_scope=None,
            category_scope=None,
            tier=None,
            description=description if description is not None else f"description of {name}",
            current_version_id=version_id,
        )
    )
    for i in range(fragments):
        store.insert_fragment(
            FragmentRow(
                fragment_id=f"{skill_id}::frag{i}",
                version_id=version_id,
                fragment_type="execution",
                sequence=i,
                content=f"{name} fragment {i} — distinct body for {skill_id}",
            )
        )
    return version_id


@pytest.fixture()
def corpus_env(tmp_path, monkeypatch):
    """Point settings at a per-test corpus store; return (store_path, fake client)."""
    store_path = str(tmp_path / "agentalloy.overgraph")
    monkeypatch.setenv("CORPUS_STORE_PATH", store_path)
    monkeypatch.setenv("TELEMETRY_DB_PATH", str(tmp_path / "telemetry.duck"))
    monkeypatch.setenv("RUNTIME_EMBEDDING_MODEL", MODEL_A)
    monkeypatch.setenv("CODE_INDEX_ENABLED", "false")
    client = FakeEmbedClient()
    monkeypatch.setattr(reembed_cli, "get_embed_client", lambda settings: client)
    return store_path, client


@pytest.fixture()
def discover_probe(monkeypatch):
    """Record every call into the normal path's discovery step."""
    calls: list[dict[str, Any]] = []
    real = reembed_cli.discover_unembedded_fragments

    def probe(store, vector_store, *, skill_id=None, force=False):
        calls.append({"skill_id": skill_id, "force": force})
        return real(store, vector_store, skill_id=skill_id, force=force)

    monkeypatch.setattr(reembed_cli, "discover_unembedded_fragments", probe)
    return calls


@pytest.fixture()
def no_service_stop(monkeypatch):
    """Fail the test if the service stop/restart path is ever taken."""

    def boom() -> str | None:
        raise AssertionError("service stop path was taken")

    monkeypatch.setattr(reembed_cli, "_stop_main_service", boom)
    return boom


def _seed_two_skills(store: Any) -> None:
    _seed_skill(store, "skill-a", "Alpha", "alpha")
    _seed_skill(store, "skill-b", "Beta", "beta")


# ---------------------------------------------------------------------------
# _corpus_content_hash
# ---------------------------------------------------------------------------


def test_corpus_content_hash_stable_and_sensitive(corpus_env, monkeypatch, tmp_path) -> None:
    store_path, _ = corpus_env
    store = open_skills()
    try:
        _seed_two_skills(store)
        h1 = reembed_cli._corpus_content_hash(store)
        assert reembed_cli._corpus_content_hash(store) == h1
    finally:
        store.close()

    # Same corpus content in a different store location → identical hash.
    monkeypatch.setenv("CORPUS_STORE_PATH", str(tmp_path / "other.overgraph"))
    monkeypatch.setenv("TELEMETRY_DB_PATH", str(tmp_path / "other.duck"))
    store2 = open_skills()
    try:
        _seed_two_skills(store2)
        assert reembed_cli._corpus_content_hash(store2) == h1
    finally:
        store2.close()

    # Any content difference → different hash.
    monkeypatch.setenv("CORPUS_STORE_PATH", str(tmp_path / "divergent.overgraph"))
    monkeypatch.setenv("TELEMETRY_DB_PATH", str(tmp_path / "divergent.duck"))
    store3 = open_skills()
    try:
        _seed_two_skills(store3)
        _seed_skill(store3, "skill-c", "Gamma", "gamma", fragments=1)
        assert reembed_cli._corpus_content_hash(store3) != h1
    finally:
        store3.close()


# ---------------------------------------------------------------------------
# main() fast path
# ---------------------------------------------------------------------------


def test_main_fast_path_skips_unchanged_corpus(corpus_env, discover_probe, no_service_stop) -> None:
    _, client = corpus_env

    store = open_skills()
    try:
        _seed_two_skills(store)
    finally:
        store.close()

    # First pass: normal path — embeds, rebuilds FTS, stamps the meta.
    # (With the default card-index mode the card stage re-discovers with
    # force=True, so the total discovery-call count here is >=1, not an
    # exact number — exact counts depend on HNSW persistence across store
    # reopens, a separate known issue.)
    assert reembed_cli.main([]) == reembed_cli.EXIT_OK
    assert client.calls, "first pass must embed"
    assert discover_probe, "first pass must enter the normal path"
    assert discover_probe[0] == {"force": False, "skill_id": None}

    store = open_skills(read_only=True)
    try:
        stamp = json.loads(store.get_meta(META_KEY_LAST_REEMBED))
    finally:
        store.close()
    assert stamp["model"] == MODEL_A
    assert stamp["card_index"] == "both"  # CLI default
    assert len(stamp["hash"]) == 64
    assert "at" in stamp

    # Second pass, corpus unchanged: fast path — no discovery, no embed,
    # no service stop.
    discover_probe.clear()
    calls_before = len(client.calls)
    assert reembed_cli.main([]) == reembed_cli.EXIT_OK
    assert discover_probe == [], "fast path must not enter the normal path"
    assert len(client.calls) == calls_before, "fast path must not embed"


def test_main_runs_normal_path_when_model_changes(
    corpus_env, discover_probe, no_service_stop
) -> None:
    _, client = corpus_env

    store = open_skills()
    try:
        _seed_two_skills(store)
    finally:
        store.close()

    assert reembed_cli.main([]) == reembed_cli.EXIT_OK

    discover_probe.clear()
    assert reembed_cli.main(["--model", MODEL_B]) == reembed_cli.EXIT_OK
    # Bypass = entered the normal path (>=1 discovery call). The exact count
    # is 1 (nothing unembedded → early return) or 2 (HNSW did not persist
    # across the reopen → full re-embed + card stage); both are correct
    # behavior for a model mismatch.
    assert len(discover_probe) >= 1, "model mismatch must bypass the fast path"

    # The mismatched model is re-stamped by a forced rebuild, restoring
    # the skip for subsequent plain runs with that model.
    discover_probe.clear()
    assert reembed_cli.main(["--force", "--model", MODEL_B]) == reembed_cli.EXIT_OK
    assert discover_probe and discover_probe[0]["force"] is True

    store = open_skills(read_only=True)
    try:
        stamp = json.loads(store.get_meta(META_KEY_LAST_REEMBED))
    finally:
        store.close()
    assert stamp["model"] == MODEL_B

    discover_probe.clear()
    assert reembed_cli.main(["--model", MODEL_B]) == reembed_cli.EXIT_OK
    assert discover_probe == [], "stamp with matching model must skip again"


def test_main_runs_normal_path_when_card_mode_changes(
    corpus_env, discover_probe, no_service_stop
) -> None:
    _, _client = corpus_env

    store = open_skills()
    try:
        _seed_two_skills(store)
    finally:
        store.close()

    assert reembed_cli.main([]) == reembed_cli.EXIT_OK  # stamps mode 'both'

    discover_probe.clear()
    assert reembed_cli.main(["--card-index", "off"]) == reembed_cli.EXIT_OK
    assert len(discover_probe) == 1, "card-mode mismatch must bypass the fast path"


def test_main_force_bypasses_fast_path(corpus_env, discover_probe, no_service_stop) -> None:
    _, client = corpus_env

    store = open_skills()
    try:
        _seed_two_skills(store)
    finally:
        store.close()

    assert reembed_cli.main([]) == reembed_cli.EXIT_OK

    discover_probe.clear()
    calls_before = len(client.calls)
    assert reembed_cli.main(["--force"]) == reembed_cli.EXIT_OK
    # --force must bypass the fast path and re-discover in force mode (the
    # card stage, when active, adds a second forced discovery).
    assert discover_probe and discover_probe[0]["force"] is True
    assert len(client.calls) > calls_before, "--force must re-embed"


def test_skill_scoped_pass_does_not_stamp(corpus_env, discover_probe, no_service_stop) -> None:
    _, _client = corpus_env

    store = open_skills()
    try:
        _seed_two_skills(store)
    finally:
        store.close()

    assert reembed_cli.main(["--skill-id", "skill-a"]) == reembed_cli.EXIT_OK

    store = open_skills(read_only=True)
    try:
        assert store.get_meta(META_KEY_LAST_REEMBED) is None
    finally:
        store.close()


def test_skill_metadata_change_invalidates_stamp(
    corpus_env, discover_probe, no_service_stop
) -> None:
    """The hash covers skill-card inputs, not just fragment bodies."""
    _, _client = corpus_env

    store = open_skills()
    try:
        _seed_two_skills(store)
    finally:
        store.close()

    assert reembed_cli.main([]) == reembed_cli.EXIT_OK

    # Edit a skill's description (a card-input field) without touching
    # any fragment body.
    store = open_skills()
    try:
        skill = store.get_skill("skill-a")
        assert skill is not None
        store.insert_skill(
            SkillRow(
                skill_id=skill.skill_id,
                canonical_name=skill.canonical_name,
                category=skill.category,
                skill_class=skill.skill_class,
                domain_tags=list(skill.domain_tags),
                deprecated=skill.deprecated,
                superseded_by=skill.superseded_by,
                always_apply=skill.always_apply,
                phase_scope=list(skill.phase_scope) if skill.phase_scope else None,
                category_scope=list(skill.category_scope) if skill.category_scope else None,
                tier=skill.tier,
                description="a completely new description",
                current_version_id=skill.current_version_id,
            )
        )
    finally:
        store.close()

    discover_probe.clear()
    assert reembed_cli.main([]) == reembed_cli.EXIT_OK
    # Bypass = entered the normal path (>=1 discovery call; a matching stamp
    # would have made it exactly 0).
    assert len(discover_probe) >= 1, "skill-metadata change must bypass the fast path"


def test_first_run_on_fresh_store_succeeds(corpus_env, discover_probe, no_service_stop) -> None:
    """No stamp yet → probe finds nothing → normal path, no crash."""
    discover_probe.clear()
    assert reembed_cli.main([]) == reembed_cli.EXIT_OK
    assert len(discover_probe) == 1


def test_settings_env_paths_resolve(tmp_path, monkeypatch) -> None:
    """Sanity: bare-name env overrides drive the store location."""
    monkeypatch.setenv("CORPUS_STORE_PATH", str(tmp_path / "env.overgraph"))
    settings = Settings()
    assert settings.corpus_store_path == str(tmp_path / "env.overgraph")
