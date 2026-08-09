"""Where a compound-engineering lesson lives, now that it is store-backed.

One module so the four consumers cannot drift: the codify gate
(``signals.predicates.eval_lessons_recorded``), the decision-source ingest
(``code_index.ingest.pipeline``), the promote path
(``install.subcommands.lessons``), and the knowledge-push dedup
(``api.knowledge_push``).

The lesson is a store artifact keyed ``(phase='qa', slug=<work-item>,
name='solution')``. Two shape decisions are load-bearing:

* **No ``.md`` suffix on the name.** The qa exit gate globs ``name: "*.md"`` and
  ``artifact_contains`` requires EVERY matching row to carry ``## Checks`` and
  ``## Review``. A ``solution.md`` row would be swept up by that glob, so writing
  the lesson would break the qa artifact gate sitting beside the codify gate.
* **The synthetic path keeps the old on-disk shape.** Ingest emits lesson chunks
  under ``docs/solutions/<slug>.md`` — the same repo-relative path the pre-store
  file had. That is what lets ``_DECISION_SOURCE_GLOBS`` and
  ``knowledge_push._solutions_slug`` (which parses the slug back out of a
  ``docs/solutions/<slug>.md::anchor`` qualified name) keep working untouched.
  This mirrors the design-artifact migration, which kept
  ``docs/design/<slug>/approach.artifact`` for exactly the same reason.
"""

from __future__ import annotations

LESSON_PHASE = "qa"
LESSON_NAME = "solution"

SOLUTIONS_PREFIX = "docs/solutions/"


def lesson_doc_path(slug: str) -> str:
    """The repo-relative path a lesson is indexed under, store-backed or on disk.

    Stable across the migration on purpose — see the module docstring.
    """
    return f"{SOLUTIONS_PREFIX}{slug}.md"
