"""T1 — ship-completion ask (phase-boundary-confirmation).

When delivery has landed (phase==ship and a docs/ship/<slug>.md record exists),
the signal layer emits a deterministic confirm directive telling the agent to ask
the user whether to reset to intake — not left to skip-able ship prose. The
directive rides the advisory injection seam under a distinct [agentalloy-confirm]
label and never writes the phase file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentalloy.api.compose_models import EmptyResult
from agentalloy.api.proxy_apply import _compose_block
from agentalloy.api.proxy_models import ProxyMessage, ProxyRequest
from agentalloy.api.proxy_signal import CONFIRM_LABEL, SignalResult, evaluate_signal
from agentalloy.orchestration.compose import ComposeOrchestrator


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


def _set_phase(tmp: Path, phase: str) -> None:
    d = tmp / ".agentalloy"
    d.mkdir(exist_ok=True, parents=True)
    (d / "phase").write_text(f"phase: {phase}\n")


def _ship_record(tmp: Path, slug: str = "some-feature") -> None:
    d = tmp / "docs" / "ship"
    d.mkdir(exist_ok=True, parents=True)
    (d / f"{slug}.md").write_text("# Ship\n")


async def test_confirm_emitted_when_ship_and_record_exists(tmp_path: Path):
    _set_phase(tmp_path, "ship")
    _ship_record(tmp_path)
    sig = await evaluate_signal(_req(), tmp_path)
    assert sig.confirm_directives, "ship + delivery record must emit a confirm directive"
    joined = "\n".join(sig.confirm_directives).lower()
    assert "intake" in joined and "ask" in joined


async def test_no_confirm_on_ship_without_record(tmp_path: Path):
    _set_phase(tmp_path, "ship")  # entered ship, no delivery record yet
    sig = await evaluate_signal(_req(), tmp_path)
    assert not sig.confirm_directives, "no prompt mid-delivery, before the record exists"


async def test_no_confirm_when_not_ship(tmp_path: Path):
    _set_phase(tmp_path, "build")
    _ship_record(tmp_path)  # a stale record from a prior item must not trigger in build
    sig = await evaluate_signal(_req(), tmp_path)
    assert not sig.confirm_directives


async def test_confirm_persists_across_ship_turns(tmp_path: Path):
    _set_phase(tmp_path, "ship")
    _ship_record(tmp_path)
    first = await evaluate_signal(_req(), tmp_path)
    second = await evaluate_signal(_req("still here"), tmp_path)
    assert first.confirm_directives and second.confirm_directives, "must not vanish after one turn"


async def test_confirm_does_not_write_phase(tmp_path: Path):
    _set_phase(tmp_path, "ship")
    _ship_record(tmp_path)
    await evaluate_signal(_req(), tmp_path)
    assert (tmp_path / ".agentalloy" / "phase").read_text() == "phase: ship\n", "no auto-reset"


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
