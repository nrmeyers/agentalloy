"""retrieve maps LMClientError subclasses onto stage-scoped codes (NXS-768).

v5.4: assembly stage is gone (no LLM in compose path). Only retrieval stage
errors are exercised here. Assembly-stage tests have been removed.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from agentalloy.api.compose_models import ComposeRequest
from agentalloy.lm_client import (
    LMModelNotLoaded,
    LMTimeout,
    LMUnavailable,
    OpenAICompatClient,
)
from agentalloy.orchestration.compose import (
    ComposeOrchestrator,
    RetrievalStageError,
)


@pytest.fixture
def req() -> ComposeRequest:
    return ComposeRequest(task="t", phase="design")


def _orchestrator() -> ComposeOrchestrator:
    from agentalloy.telemetry.writer import NullTelemetryWriter

    return ComposeOrchestrator(
        MagicMock(),
        cast(OpenAICompatClient, MagicMock()),
        MagicMock(),  # vector_store — retrieve_domain_candidates is patched in these tests
        NullTelemetryWriter(),
        embedding_model="fake-embed",
    )


@pytest.mark.asyncio
async def test_retrieve_embedding_model_unavailable(req: ComposeRequest) -> None:
    orch = _orchestrator()
    with (
        patch(
            "agentalloy.orchestration.compose.retrieve_domain_candidates",
            side_effect=LMModelNotLoaded("fake-embed", []),
        ),
        pytest.raises(RetrievalStageError) as ei,
    ):
        await orch.retrieve(req)
    assert ei.value.code == "embedding_model_unavailable"


@pytest.mark.asyncio
async def test_retrieve_embedding_failed_on_generic_lm_error(req: ComposeRequest) -> None:
    orch = _orchestrator()
    with (
        patch(
            "agentalloy.orchestration.compose.retrieve_domain_candidates",
            side_effect=LMUnavailable("connection refused"),
        ),
        pytest.raises(RetrievalStageError) as ei,
    ):
        await orch.retrieve(req)
    assert ei.value.code == "embedding_failed"


@pytest.mark.asyncio
async def test_retrieve_timeout_maps_to_embedding_failed(req: ComposeRequest) -> None:
    orch = _orchestrator()
    with (
        patch(
            "agentalloy.orchestration.compose.retrieve_domain_candidates",
            side_effect=LMTimeout("read timed out"),
        ),
        pytest.raises(RetrievalStageError) as ei,
    ):
        await orch.retrieve(req)
    assert ei.value.code == "embedding_failed"


@pytest.mark.asyncio
async def test_retrieve_unexpected_error_maps_to_store_unavailable(req: ComposeRequest) -> None:
    orch = _orchestrator()
    with (
        patch(
            "agentalloy.orchestration.compose.retrieve_domain_candidates",
            side_effect=RuntimeError("ladybug crashed"),
        ),
        pytest.raises(RetrievalStageError) as ei,
    ):
        await orch.retrieve(req)
    assert ei.value.code == "store_unavailable"
