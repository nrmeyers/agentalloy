"""Compound engineering: QA-gated lesson capture and promotion to skills.

When a bug is found and fixed during QA, a lesson is captured.
Lessons accumulate; when a lesson is referenced enough times,
it's promoted to an injected skill (dedup-gated).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentalloy.skill_engine import Skill, SkillEngine
from agentalloy.state_store import StateStore


@dataclass
class Lesson:
    """A captured QA lesson."""

    slug: str
    title: str
    body: str
    phase: str = "qa"
    domain_tags: list[str] = field(default_factory=list)
    reference_count: int = 0
    promoted: bool = False


class CompoundEngine:
    """Manages lesson capture, tracking, and skill promotion."""

    PROMOTION_THRESHOLD = 3  # references needed to promote

    def __init__(self, state_store: StateStore, skill_engine: SkillEngine) -> None:
        self.store = state_store
        self.skill_engine = skill_engine
        self._init_tables()

    def _init_tables(self) -> None:
        """Create lessons table if not exists."""
        self.store.conn.execute("""
            CREATE TABLE IF NOT EXISTS lessons (
                slug TEXT PRIMARY KEY,
                title TEXT,
                body TEXT,
                phase TEXT DEFAULT 'qa',
                domain_tags TEXT DEFAULT '[]',
                reference_count INTEGER DEFAULT 0,
                promoted INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    def capture_lesson(
        self,
        slug: str,
        title: str,
        body: str,
        domain_tags: list[str] | None = None,
    ) -> Lesson:
        """Capture a new QA lesson."""
        import json

        tags = domain_tags or []
        self.store.conn.execute(
            """
            INSERT INTO lessons (slug, title, body, domain_tags)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                title = excluded.title,
                body = excluded.body,
                domain_tags = excluded.domain_tags
            """,
            [slug, title, body, json.dumps(tags)],
        )
        return Lesson(slug=slug, title=title, body=body, domain_tags=tags)

    def reference_lesson(self, slug: str) -> Lesson | None:
        """Record a reference to a lesson. Returns updated lesson or None."""
        import json

        self.store.conn.execute(
            "UPDATE lessons SET reference_count = reference_count + 1 WHERE slug = ?",
            [slug],
        )
        row = self.store.conn.execute(
            """
            SELECT slug, title, body, phase, domain_tags, reference_count, promoted
            FROM lessons WHERE slug = ?
            """,
            [slug],
        ).fetchone()

        if row is None:
            return None

        lesson = Lesson(
            slug=row[0],
            title=row[1],
            body=row[2],
            phase=row[3],
            domain_tags=json.loads(row[4]) if row[4] else [],
            reference_count=row[5],
            promoted=bool(row[6]),
        )

        # Auto-promote if threshold reached
        if lesson.reference_count >= self.PROMOTION_THRESHOLD and not lesson.promoted:
            self.promote_lesson(slug)
            lesson.promoted = True

        return lesson

    def promote_lesson(self, slug: str) -> Skill | None:
        """Promote a lesson to an injected skill (dedup-gated)."""
        import json

        row = self.store.conn.execute(
            "SELECT slug, title, body, domain_tags FROM lessons WHERE slug = ?",
            [slug],
        ).fetchone()
        if row is None:
            return None

        lesson_slug, title, body, tags_json = row
        domain_tags = json.loads(tags_json) if tags_json else []

        # Dedup gate: don't add if skill already exists
        skill_id = f"lesson-{lesson_slug}"
        if skill_id in self.skill_engine.skills:
            return None

        skill = Skill(
            id=skill_id,
            name=title,
            description=f"Auto-promoted from QA lesson: {lesson_slug}",
            body=body,
            phases=["build", "qa"],
            domain_tags=domain_tags,
            skill_class="domain",
            pack="lessons",
        )
        self.skill_engine.add_skill(skill)

        # Mark as promoted
        self.store.conn.execute("UPDATE lessons SET promoted = 1 WHERE slug = ?", [slug])
        return skill

    def list_lessons(self) -> list[Lesson]:
        """List all captured lessons."""
        import json

        rows = self.store.conn.execute(
            """
            SELECT slug, title, body, phase, domain_tags, reference_count, promoted
            FROM lessons ORDER BY reference_count DESC
            """
        ).fetchall()

        return [
            Lesson(
                slug=r[0],
                title=r[1],
                body=r[2],
                phase=r[3],
                domain_tags=json.loads(r[4]) if r[4] else [],
                reference_count=r[5],
                promoted=bool(r[6]),
            )
            for r in rows
        ]

    def get_lesson(self, slug: str) -> Lesson | None:
        """Get a lesson by slug."""
        import json

        row = self.store.conn.execute(
            """
            SELECT slug, title, body, phase, domain_tags, reference_count, promoted
            FROM lessons WHERE slug = ?
            """,
            [slug],
        ).fetchone()
        if row is None:
            return None
        return Lesson(
            slug=row[0],
            title=row[1],
            body=row[2],
            phase=row[3],
            domain_tags=json.loads(row[4]) if row[4] else [],
            reference_count=row[5],
            promoted=bool(row[6]),
        )
