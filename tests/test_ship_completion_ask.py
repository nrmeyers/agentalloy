"""T1 — ship-completion ask (phase-boundary-confirmation).

When delivery has landed (phase==ship and a delivery artifact exists in the
store), the signal layer emits a deterministic confirm directive telling the
agent to ask the user whether to reset to intake — not left to skip-able ship
prose. The directive rides the advisory injection seam under a distinct
[agentalloy-confirm] label and never writes the phase file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentalloy.api.compose_models import EmptyResult
from agentalloy.api.proxy_apply import _compose_block
from agentalloy.api.proxy_models import ProxyMessage, ProxyRequest
from agentalloy.api.proxy_signal import CONFIRM_LABEL, SignalResult, evaluate_signal
from agentalloy.orchestration.compose import ComposeOrchestrator
from agentalloy.signals.skill_loader import _read_phase  # pyright: ignore[reportPrivateUsage]
from tests.support import seed_phase


class _NullOrch(ComposeOrchestrator):
    """No compose legs — isolates the confirm block at the inject seam."""

    def __init__(self) -> None:  # noqa: D107 — deliberately no super().__init__
        pass

    async def compose(self, req: Any, **_kw: object) -> Any:
        return EmptyResult(task="t", phase="ship", system_fragments=[])


def _req(text: str = "continue") -> ProxyRequest:
    return ProxyRequest(
        model="gpt-4",
        messages=[ProxyMessage(role="user", content=text)],
        tools=[{"name": "Read", "description": "read", "input_schema": {}}],
    )


def _scoped_store(tmp: Path):
    """The bound process store, re-scoped to ``tmp`` exactly like the signal
    layer reads it (repo slug + stream id). Phase/cursor/contract/artifact rows
    seeded here are what ``evaluate_signal`` sees for this repo root."""
    from agentalloy.api.state_router import scoped_state_store
    from agentalloy.storage.state_store import process_store

    store = process_store()
    assert store is not None, "no state store bound — is _bound_state_store active?"
    return scoped_state_store(store, tmp)


def _seed_delivery(
    tmp_path: Path,
    slug: str = "some-feature",
    *,
    delivery: bool = True,
    phase: str = "ship",
) -> None:
    """Seed ship-phase state the way the product writes it: phase row in the
    store, work-item cursor, contract, and (optionally) the ``delivery``
    artifact — all in the repo+stream scope ``evaluate_signal`` reads."""
    d = tmp_path / ".agentalloy"
    d.mkdir(exist_ok=True, parents=True)
    seed_phase(tmp_path, phase)
    (d / "cursor").write_text(slug, encoding="utf-8")
    view = _scoped_store(tmp_path)
    view.put_contract(
        slug,
        phase=phase,
        slug=slug,
        route="full",
        scope_touches=[],
        scope_avoids=[],
        success_criteria=[],
        body=f"# {slug}\n\nbody\n",
    )
    if delivery:
        view.set_artifact(phase, slug, "delivery", f"# Ship record for {slug}\n")


async def test_confirm_emitted_when_ship_and_record_exists(tmp_path: Path):
    _seed_delivery(tmp_path)
    sig = await evaluate_signal(_req(), tmp_path)
    assert sig.confirm_directives, "ship + delivery artifact must emit a confirm directive"
    joined = "\n".join(sig.confirm_directives).lower()
    assert "intake" in joined and "ask" in joined


async def test_no_confirm_on_ship_without_record(tmp_path: Path):
    _seed_delivery(tmp_path, delivery=False)  # ship phase, no delivery artifact yet
    sig = await evaluate_signal(_req(), tmp_path)
    assert not sig.confirm_directives, "no prompt mid-delivery, before the artifact exists"


async def test_no_confirm_when_not_ship(tmp_path: Path):
    # A stale delivery artifact from a prior item must not trigger in build
    _seed_delivery(tmp_path, phase="build")
    _scoped_store(tmp_path).set_artifact(
        "ship",
        "some-feature",
        "delivery",
        "# Ship record",
    )
    sig = await evaluate_signal(_req(), tmp_path)
    assert not sig.confirm_directives


async def test_confirm_persists_across_ship_turns(tmp_path: Path):
    _seed_delivery(tmp_path)
    first = await evaluate_signal(_req(), tmp_path)
    second = await evaluate_signal(_req("still here"), tmp_path)
    assert first.confirm_directives and second.confirm_directives, "must not vanish after one turn"


async def test_confirm_does_not_write_phase(tmp_path: Path):
    _seed_delivery(tmp_path)
    await evaluate_signal(_req(), tmp_path)
    assert _read_phase(tmp_path) == "ship", "no auto-reset"


def test_confirm_label_is_distinct():
    # Distinct from the gate-advisory [agentalloy-eval] label (clean telemetry).
    assert CONFIRM_LABEL == "agentalloy-confirm"


async def test_confirm_block_reaches_injected_text(tmp_path: Path):
    # AC-1/AC-6: drive the shared apply seam, not the skill — the directive must
    # surface as an [agentalloy-confirm] block in the injected text.
    sig = SignalResult(
        should_compose=True,
        announce=False,
        announce_cursor=False,
        phase="ship",
        repo=str(tmp_path),
        confirm_directives=["Delivery landed — ASK the user whether to reset to intake."],
    )
    block = await _compose_block(sig, _NullOrch())
    assert f"[{CONFIRM_LABEL}]" in block.text and f"[/{CONFIRM_LABEL}]" in block.text
    assert "intake" in block.text.lower()
