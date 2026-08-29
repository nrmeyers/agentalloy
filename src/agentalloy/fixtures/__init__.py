"""Dev/test fixture loader — wipes and re-seeds the corpus store with a small fixed corpus."""

from __future__ import annotations

from agentalloy.fixtures.loader import FIXTURES_ROOT, load_fixtures

__all__ = ["FIXTURES_ROOT", "load_fixtures"]
