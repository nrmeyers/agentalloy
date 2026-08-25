"""Where a compound-engineering lesson lives, now that it is store-backed.

One module so the four consumers cannot drift: the codify gate
(``signals.predicates.eval_lessons_recorded``), the decision-source ingest
(``code_index.ingest.pipeline``), the promote path
(``install.subcommands.lessons``), and the knowledge-push dedup
(``api.knowledge_push``).

The lesson is a store artifact keyed ``(phase='qa', slug=<work-item>,
name='solution')``. Two shape decisions are load-bearing:

* **The name is the bare word ``solution`` — no ``.artifact`` suffix.** The qa
  exit gate globs ``name: "*.artifact"`` and sums the byte size of every
  matching row, and the state leg reads the first matching row as the qa report
  (parsing ``## Routed Findings``). A ``solution.artifact`` row would be swept
  into both — its bytes would count toward the report's size floor and it could
  shadow the report — so the lesson must not match that glob.
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
