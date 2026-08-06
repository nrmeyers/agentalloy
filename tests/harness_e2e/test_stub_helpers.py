"""Unit coverage for the stub's request-inspection helpers.

Deliberately NOT marked ``harness_e2e``: these need no binaries and no
sockets, so they run in the default suite. The matrix's assertions are only
as good as these extractors — a ``system_texts`` that silently returned ``[]``
for a surface would turn the leg-3 check into a no-op.
"""

from __future__ import annotations

from tests.harness_e2e.upstream_stub import (
    LEG3_BLOCK,
    CapturedRequest,
    system_texts,
    user_texts,
)

BLOCK = "<!-- BEGIN AGENTALLOY-CONTEXT phase=build -->prose<!-- END AGENTALLOY-CONTEXT -->"


def test_system_texts_reads_responses_instructions() -> None:
    req = CapturedRequest(path="/v1/responses", payload={"instructions": BLOCK, "input": "hi"})
    assert system_texts([req]) == [BLOCK]


def test_system_texts_reads_chat_completions_system_messages() -> None:
    req = CapturedRequest(
        path="/v1/chat/completions",
        payload={
            "messages": [
                {"role": "system", "content": BLOCK},
                {"role": "user", "content": "hi"},
            ]
        },
    )
    assert system_texts([req]) == [BLOCK]


def test_system_texts_joins_multiple_system_messages_in_order() -> None:
    req = CapturedRequest(
        path="/v1/chat/completions",
        payload={
            "messages": [
                {"role": "system", "content": "first"},
                {"role": "developer", "content": "second"},
                {"role": "user", "content": "hi"},
            ]
        },
    )
    assert system_texts([req]) == ["first\nsecond"]


def test_system_texts_reads_anthropic_system_field_both_forms() -> None:
    as_string = CapturedRequest(path="/v1/messages", payload={"system": BLOCK})
    as_blocks = CapturedRequest(
        path="/v1/messages", payload={"system": [{"type": "text", "text": BLOCK}]}
    )
    assert system_texts([as_string, as_blocks]) == [BLOCK, BLOCK]


def test_system_texts_is_one_entry_per_request_even_when_bare() -> None:
    """Per-request alignment is the point: the matrix asserts on every turn."""
    reqs = [
        CapturedRequest(path="/v1/chat/completions", payload={"messages": [{"role": "user"}]}),
        CapturedRequest(path="/v1/responses", payload={"instructions": BLOCK}),
        CapturedRequest(path="/v1/chat/completions", payload={}),
    ]
    assert system_texts(reqs) == ["", BLOCK, ""]


def test_user_texts_still_reads_the_user_leg_not_the_system_leg() -> None:
    req = CapturedRequest(
        path="/v1/chat/completions",
        payload={
            "messages": [
                {"role": "system", "content": BLOCK},
                {"role": "user", "content": "hi"},
            ]
        },
    )
    assert user_texts([req]) == ["hi"]
    assert system_texts([req]) == [BLOCK]


class TestLeg3BlockDetector:
    """The matrix's leg-3 detector must recognize BOTH delivery formats.

    Leg 3 (the SDD workflow prose) moved to system-message injection under the
    D3 delimited-block tag (``<agentalloy-instructions phase=\"…\">``) — the
    HTML-style marker is the legacy, user-message form. The matrix counts the
    detector's matches, so a detector that misses either format silently reads
    every leg-3 block as absent and turns the every-turn assert into a broken
    no-op (the #499 deliver-once regression it exists to catch). These pin the
    exact delivered begin markers.
    """

    def test_matches_html_style_marker_exactly_once(self) -> None:
        assert LEG3_BLOCK.findall(BLOCK) == ["BEGIN AGENTALLOY-CONTEXT phase=build"]

    def test_matches_xml_delimited_tag_exactly_once(self) -> None:
        # Delivered by proxy_injection's ANTHROPIC_INSTRUCTIONS_BEGIN/SUFFIX
        # (<agentalloy-instructions phase=\"intake\">…</agentalloy-instructions>).
        leg = '<agentalloy-instructions phase="intake">prose</agentalloy-instructions>'
        assert LEG3_BLOCK.findall(leg) == ['<agentalloy-instructions phase="intake">']

    def test_does_not_match_a_bare_phase_quote(self) -> None:
        # The malformed literal pattern (``phase=\">``) must never be the
        # vehicle again — the real tag always carries a phase value.
        assert (
            LEG3_BLOCK.findall('<agentalloy-instructions phase="></agentalloy-instructions>') == []
        )

    def test_counts_each_block_independently(self) -> None:
        leg = (
            "prefix "
            '<agentalloy-instructions phase="build">a</agentalloy-instructions> '
            '<agentalloy-instructions phase="build">b</agentalloy-instructions>'
        )
        assert len(LEG3_BLOCK.findall(leg)) == 2
