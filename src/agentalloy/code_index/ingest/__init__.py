"""Ingest pipeline for the code-index module: parse → graph → embed → fts.

``pipeline.run_index_job`` orchestrates; ``embed_text`` / ``markdown`` are
pure composition helpers; ``watch`` is the optional debounced re-index
trigger. Importing this package pulls in the vendored tree-sitter engine
(via the facade) — keep imports lazy behind the module toggle.
"""

from __future__ import annotations

from agentalloy.code_index.ingest.pipeline import IndexResult, run_index_job

__all__ = ["IndexResult", "run_index_job"]
