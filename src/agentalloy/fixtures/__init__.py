"""Dev/test fixture loader — wipes and re-seeds the skill store (agentalloy.duck) with a small fixed corpus."""

from __future__ import annotations

from agentalloy.fixtures.loader import FIXTURES_ROOT, load_fixtures

__all__ = ["FIXTURES_ROOT", "load_fixtures"]
