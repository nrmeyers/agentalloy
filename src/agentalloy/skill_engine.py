"""Skill engine: dynamic skill selection and composition.

The LFM infers which skills are needed based on the task (R1).
Skills are auto-injected into prompts via the steering proxy (T9).

Supports two skill sources:
- Built-in SKILL_CORPUS (33 hand-curated skills for quick start)
- Full v1 corpus (355 skills / 41 packs) via corpus_importer
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# v1 fragment taxonomy — the LFM selects skills + types; the engine expands
# the selection to fragments deterministically.
FRAGMENT_TYPES = ("execution", "example", "rationale", "verification", "guardrail", "setup")


@dataclass
class SkillFragment:
    """One instruction fragment of a skill (v1 fragment model).

    v1 sequences are 1-based; the first fragment (lowest sequence) is the
    head and is always included in an assembly (head-reserve).
    """

    fragment_id: str
    fragment_type: str
    sequence: int
    content: str


@dataclass
class Skill:
    """A skill that can be composed into prompts."""

    id: str = ""
    name: str = ""
    description: str = ""
    body: str = ""
    phases: list[str] = field(default_factory=list)
    domain_tags: list[str] = field(default_factory=list)
    skill_class: str = "domain"
    pack: str = ""
    fragments: list[SkillFragment] = field(default_factory=list)

    @property
    def phase(self) -> str:
        """Primary phase (first in list, or 'build' default)."""
        return self.phases[0] if self.phases else "build"

    @property
    def instructions(self) -> str:
        """Alias for body (backward compat)."""
        return self.body


# Built-in skill corpus (M1: 33 hand-curated skills)
SKILL_CORPUS: dict[str, Skill] = {
    # --- Core skills ---
    "code-search": Skill(
        id="code-search",
        name="code-search",
        description="Semantic code search across the codebase",
        body="Use code_search tool to find relevant code snippets.",
        phases=["spec", "design", "plan", "build", "qa", "ship"],
        domain_tags=["retrieval", "code"],
        skill_class="system",
    ),
    "contract-management": Skill(
        id="contract-management",
        name="contract-management",
        description="Manage contracts and artifacts",
        body="Use contract_add and artifact_record tools.",
        phases=["spec", "design", "plan", "build", "qa", "ship"],
        domain_tags=["state", "contracts"],
        skill_class="system",
    ),
    "phase-advance": Skill(
        id="phase-advance",
        name="phase-advance",
        description="Advance through lifecycle phases",
        body="Use phase_advance tool to move to next phase.",
        phases=["spec", "design", "plan", "build", "qa", "ship"],
        domain_tags=["lifecycle", "state"],
        skill_class="system",
    ),
    # --- Spec phase ---
    "requirements-gathering": Skill(
        id="requirements-gathering",
        name="requirements-gathering",
        description="Elicit and document user requirements",
        body=(
            "Ask clarifying questions. Document functional and non-functional requirements. "
            "Use contract_add to record key decisions. Identify acceptance criteria."
        ),
        phases=["spec"],
        domain_tags=["requirements", "elicitation"],
    ),
    "acceptance-criteria": Skill(
        id="acceptance-criteria",
        name="acceptance-criteria",
        description="Define testable acceptance criteria",
        body=(
            "Write acceptance criteria in Given/When/Then format. "
            "Each criterion should be independently testable. "
            "Use artifact_record to capture the AC document."
        ),
        phases=["spec"],
        domain_tags=["testing", "requirements"],
    ),
    "user-stories": Skill(
        id="user-stories",
        name="user-stories",
        description="Write user stories with acceptance criteria",
        body=(
            "Format: As a [role], I want [goal], so that [benefit]. "
            "Include acceptance criteria for each story. "
            "Prioritize using MoSCoW (Must/Should/Could/Won't)."
        ),
        phases=["spec"],
        domain_tags=["requirements", "agile"],
    ),
    # --- Design phase ---
    "architecture-design": Skill(
        id="architecture-design",
        name="architecture-design",
        description="Design system architecture and component structure",
        body=(
            "Identify components, their responsibilities, and interfaces. "
            "Choose architectural patterns (layered, microservices, event-driven). "
            "Document decisions using ADRs (Architecture Decision Records)."
        ),
        phases=["design"],
        domain_tags=["architecture", "design"],
    ),
    "api-design": Skill(
        id="api-design",
        name="api-design",
        description="Design RESTful or GraphQL APIs",
        body=(
            "Define endpoints, request/response schemas, error codes. "
            "Follow REST conventions: proper HTTP methods, status codes, pagination. "
            "Document with OpenAPI/Swagger or GraphQL schema."
        ),
        phases=["design"],
        domain_tags=["api", "design", "rest"],
    ),
    "data-modeling": Skill(
        id="data-modeling",
        name="data-modeling",
        description="Design database schemas and data models",
        body=(
            "Identify entities, relationships, and constraints. "
            "Choose appropriate data types and indexes. "
            "Consider normalization vs. denormalization trade-offs. "
            "Document with ER diagrams or schema definitions."
        ),
        phases=["design"],
        domain_tags=["database", "design", "schema"],
    ),
    "security-design": Skill(
        id="security-design",
        name="security-design",
        description="Design security controls and threat mitigations",
        body=(
            "Identify attack vectors and threat models. "
            "Design authentication, authorization, and encryption. "
            "Follow principle of least privilege. "
            "Document security requirements and controls."
        ),
        phases=["design"],
        domain_tags=["security", "design", "auth"],
    ),
    # --- Plan phase ---
    "task-decomposition": Skill(
        id="task-decomposition",
        name="task-decomposition",
        description="Break design into implementable tasks",
        body=(
            "Decompose features into small, independent tasks. "
            "Estimate effort (story points or hours). "
            "Identify dependencies and critical path. "
            "Prioritize by value and risk."
        ),
        phases=["plan"],
        domain_tags=["planning", "tasks"],
    ),
    "test-strategy": Skill(
        id="test-strategy",
        name="test-strategy",
        description="Define testing approach and coverage targets",
        body=(
            "Identify test levels: unit, integration, e2e, performance. "
            "Define coverage targets and test data requirements. "
            "Plan test automation strategy. "
            "Document test cases for critical paths."
        ),
        phases=["plan"],
        domain_tags=["testing", "planning"],
    ),
    "risk-assessment": Skill(
        id="risk-assessment",
        name="risk-assessment",
        description="Identify and mitigate project risks",
        body=(
            "List technical, schedule, and resource risks. "
            "Assess probability and impact (High/Medium/Low). "
            "Define mitigation strategies for each risk. "
            "Document in risk register."
        ),
        phases=["plan"],
        domain_tags=["risk", "planning"],
    ),
    # --- Build phase ---
    "python-best-practices": Skill(
        id="python-best-practices",
        name="python-best-practices",
        description="Write idiomatic, maintainable Python code",
        body=(
            "Follow PEP 8 style guide. Use type hints. "
            "Write docstrings for public functions/classes. "
            "Use dataclasses, enums, and pathlib where appropriate. "
            "Handle exceptions explicitly, avoid bare except."
        ),
        phases=["build"],
        domain_tags=["python", "coding"],
    ),
    "rust-best-practices": Skill(
        id="rust-best-practices",
        name="rust-best-practices",
        description="Write safe, idiomatic Rust code",
        body=(
            "Use Result/Option instead of panics. "
            "Prefer &str over String for function parameters. "
            "Use #[derive] for common traits. "
            "Run cargo clippy and cargo fmt. Handle errors with ? operator."
        ),
        phases=["build"],
        domain_tags=["rust", "coding"],
    ),
    "error-handling": Skill(
        id="error-handling",
        name="error-handling",
        description="Implement robust error handling and recovery",
        body=(
            "Define custom exception/error types. "
            "Provide meaningful error messages with context. "
            "Implement retry logic for transient failures. "
            "Log errors with stack traces for debugging."
        ),
        phases=["build"],
        domain_tags=["error-handling", "coding"],
    ),
    "logging": Skill(
        id="logging",
        name="logging",
        description="Implement structured logging for observability",
        body=(
            "Use structured logging (JSON format). "
            "Include correlation IDs for request tracing. "
            "Log at appropriate levels: DEBUG, INFO, WARN, ERROR. "
            "Avoid logging sensitive information."
        ),
        phases=["build"],
        domain_tags=["logging", "observability"],
    ),
    "unit-testing": Skill(
        id="unit-testing",
        name="unit-testing",
        description="Write effective unit tests",
        body=(
            "Test one behavior per test. Use descriptive test names. "
            "Follow Arrange-Act-Assert pattern. "
            "Mock external dependencies. Aim for >80% coverage. "
            "Test edge cases and error paths."
        ),
        phases=["build", "qa"],
        domain_tags=["testing", "coding"],
    ),
    "integration-testing": Skill(
        id="integration-testing",
        name="integration-testing",
        description="Write integration tests for component interactions",
        body=(
            "Test component boundaries and contracts. "
            "Use real dependencies when possible, test containers for databases. "
            "Verify data flows and state changes. "
            "Test error scenarios and timeouts."
        ),
        phases=["build", "qa"],
        domain_tags=["testing", "integration"],
    ),
    "code-review": Skill(
        id="code-review",
        name="code-review",
        description="Conduct effective code reviews",
        body=(
            "Check for correctness, security, and performance. "
            "Verify error handling and edge cases. "
            "Ensure code follows project conventions. "
            "Provide constructive feedback with suggestions."
        ),
        phases=["build", "qa"],
        domain_tags=["review", "quality"],
    ),
    # --- QA phase ---
    "test-execution": Skill(
        id="test-execution",
        name="test-execution",
        description="Execute test suites and analyze results",
        body=(
            "Run all test levels: unit, integration, e2e. "
            "Analyze failures and identify root causes. "
            "Verify acceptance criteria are met. "
            "Document test results and defects."
        ),
        phases=["qa"],
        domain_tags=["testing", "qa"],
    ),
    "performance-testing": Skill(
        id="performance-testing",
        name="performance-testing",
        description="Test system performance and scalability",
        body=(
            "Define performance requirements (latency, throughput). "
            "Run load tests with realistic traffic patterns. "
            "Identify bottlenecks and optimization opportunities. "
            "Document performance characteristics."
        ),
        phases=["qa"],
        domain_tags=["performance", "testing"],
    ),
    "security-testing": Skill(
        id="security-testing",
        name="security-testing",
        description="Test for security vulnerabilities",
        body=(
            "Run static analysis (SAST) and dependency scanning. "
            "Test authentication and authorization controls. "
            "Verify input validation and sanitization. "
            "Check for common vulnerabilities (OWASP Top 10)."
        ),
        phases=["qa"],
        domain_tags=["security", "testing"],
    ),
    "bug-reporting": Skill(
        id="bug-reporting",
        name="bug-reporting",
        description="Document defects with clear reproduction steps",
        body=(
            "Include steps to reproduce, expected vs actual behavior. "
            "Attach logs, screenshots, or test data. "
            "Classify severity (Critical/High/Medium/Low). "
            "Assign to appropriate owner."
        ),
        phases=["qa"],
        domain_tags=["bug", "qa"],
    ),
    # --- Ship phase ---
    "deployment": Skill(
        id="deployment",
        name="deployment",
        description="Plan and execute deployment procedures",
        body=(
            "Define deployment steps and rollback plan. "
            "Verify environment configuration and secrets. "
            "Run smoke tests after deployment. "
            "Monitor for errors and performance issues."
        ),
        phases=["ship"],
        domain_tags=["deployment", "operations"],
    ),
    "documentation": Skill(
        id="documentation",
        name="documentation",
        description="Write user and developer documentation",
        body=(
            "Write README with setup instructions. "
            "Document APIs with examples. "
            "Create user guides and tutorials. "
            "Maintain changelog and version notes."
        ),
        phases=["ship", "build"],
        domain_tags=["documentation", "writing"],
    ),
    "monitoring": Skill(
        id="monitoring",
        name="monitoring",
        description="Set up monitoring and alerting",
        body=(
            "Define key metrics (latency, error rate, throughput). "
            "Set up dashboards for visibility. "
            "Configure alerts for critical thresholds. "
            "Document on-call procedures."
        ),
        phases=["ship"],
        domain_tags=["monitoring", "operations"],
    ),
    "retrospective": Skill(
        id="retrospective",
        name="retrospective",
        description="Conduct project retrospective",
        body=(
            "Identify what went well, what didn't, and action items. "
            "Gather feedback from all stakeholders. "
            "Document lessons learned. "
            "Plan improvements for next iteration."
        ),
        phases=["ship"],
        domain_tags=["retrospective", "process"],
    ),
    # --- Cross-cutting ---
    "git-workflow": Skill(
        id="git-workflow",
        name="git-workflow",
        description="Follow Git branching and commit conventions",
        body=(
            "Use feature branches for isolated work. "
            "Write clear commit messages (imperative mood). "
            "Keep commits atomic and focused. "
            "Rebase before merging to maintain clean history."
        ),
        phases=["build"],
        domain_tags=["git", "workflow"],
    ),
    "dependency-management": Skill(
        id="dependency-management",
        name="dependency-management",
        description="Manage project dependencies and versions",
        body=(
            "Pin dependency versions for reproducibility. "
            "Regularly update dependencies for security patches. "
            "Audit dependencies for vulnerabilities. "
            "Minimize dependency count to reduce attack surface."
        ),
        phases=["build"],
        domain_tags=["dependencies", "security"],
    ),
    "refactoring": Skill(
        id="refactoring",
        name="refactoring",
        description="Improve code structure without changing behavior",
        body=(
            "Extract methods to reduce complexity. "
            "Rename for clarity. Remove dead code. "
            "Apply design patterns where appropriate. "
            "Ensure tests pass after each refactoring step."
        ),
        phases=["build", "qa"],
        domain_tags=["refactoring", "quality"],
    ),
    "debugging": Skill(
        id="debugging",
        name="debugging",
        description="Systematically diagnose and fix bugs",
        body=(
            "Reproduce the issue consistently. "
            "Read error messages and stack traces carefully. "
            "Use binary search (git bisect) to find regressions. "
            "Add logging to trace execution flow. Fix root cause, not symptoms."
        ),
        phases=["build", "qa"],
        domain_tags=["debugging", "problem-solving"],
    ),
}


class SkillEngine:
    """Dynamic skill engine: select and compose skills based on task.

    Supports loading from:
    - Built-in SKILL_CORPUS (33 skills)
    - Full v1 corpus via corpus_importer (355 skills)
    """

    def __init__(self, load_corpus: bool = False) -> None:
        self.skills: dict[str, Skill] = dict(SKILL_CORPUS)
        self._packs: dict[str, Any] = {}
        self._corpus_loaded = False
        if load_corpus:
            self.load_full_corpus()

    def load_full_corpus(self, packs_dir: str | None = None) -> int:
        """Load the full v1 corpus (355 skills / 41 packs).

        Returns the number of skills loaded.
        """
        from pathlib import Path

        from agentalloy.corpus_importer import import_corpus

        packs_path = Path(packs_dir) if packs_dir else None
        skills, packs = import_corpus(packs_path)

        for skill in skills:
            self.skills[skill.id] = skill
        self._packs = packs

        self._corpus_loaded = True
        return len(skills)

    @property
    def corpus_loaded(self) -> bool:
        return self._corpus_loaded

    def get_skill_for(
        self,
        task: str,
        phase: str,
        k: int = 30,
    ) -> list[Skill]:
        """Select skill candidates for a task.

        The LFM will infer which skills are needed (R1).
        For M1, return skills matching the phase + domain tags.
        """
        candidates = [skill for skill in self.skills.values() if phase in skill.phases]

        task_lower = task.lower()
        for skill in self.skills.values():
            if skill not in candidates and phase in skill.phases:
                continue
            if skill not in candidates:
                if any(tag in task_lower for tag in skill.domain_tags):
                    candidates.append(skill)

        return candidates[:k]

    def compose_instructions(self, skills: list[Skill]) -> str:
        """Compose skill instructions into a single prompt injection."""
        if not skills:
            return ""

        parts = ["# Active Skills\n"]
        for skill in skills:
            parts.append(f"## {skill.name}\n")
            parts.append(f"{skill.description}\n\n")
            body = skill.body or skill.instructions
            if body:
                parts.append(f"{body}\n\n")

        return "\n".join(parts)

    def add_skill(self, skill: Skill) -> None:
        """Add a skill to the corpus."""
        self.skills[skill.id or skill.name] = skill

    def list_skills(self) -> list[Skill]:
        """List all available skills."""
        return list(self.skills.values())

    def skills_for_phase(self, phase: str) -> list[Skill]:
        """Return all skills applicable to a phase."""
        return [s for s in self.skills.values() if phase in s.phases]

    # --- Fragment-native selection (LFM picks packs → skills + types, the
    # engine expands deterministically) ---

    def pack_catalog(self) -> list[dict[str, Any]]:
        """One row per pack: name, description, skill count.

        The LFM's first selection step — it picks 1-4 packs, then asks for
        their skills. Empty when only the built-in corpus is loaded.
        """
        if not self._packs:
            return []
        counts: dict[str, int] = {}
        for skill in self.skills.values():
            if skill.pack:
                counts[skill.pack] = counts.get(skill.pack, 0) + 1
        return [
            {
                "pack": name,
                "description": (meta.description or "")[:160],
                "skills": counts.get(name, 0),
            }
            for name, meta in sorted(self._packs.items())
        ]

    def skills_catalog(self, packs: list[str] | None = None) -> list[dict[str, Any]]:
        """Skill rows for the given packs (all skills when packs is None).

        Each row carries the fragment-type breakdown so the LFM can decide
        which fragment types a task needs — the selection unit is
        skill + types, never an individual fragment.
        """
        if packs is None:
            selected = list(self.skills.values())
        else:
            wanted = {str(p) for p in packs}
            selected = [s for s in self.skills.values() if s.pack in wanted]
        selected.sort(key=lambda s: (s.pack, s.id))
        rows: list[dict[str, Any]] = []
        for skill in selected:
            breakdown: dict[str, int] = {}
            for frag in skill.fragments:
                breakdown[frag.fragment_type] = breakdown.get(frag.fragment_type, 0) + 1
            if not breakdown and skill.body:
                breakdown["body"] = 1
            rows.append(
                {
                    "id": skill.id,
                    "name": skill.name,
                    "description": skill.description[:140],
                    "fragments": breakdown,
                    "phases": skill.phases,
                }
            )
        return rows

    def assemble_skill(
        self,
        skills: list[str],
        types: list[str] | None = None,
        phase: str = "",
    ) -> dict[str, Any]:
        """Deterministic expansion of the LFM's skill + type selection.

        Same input, byte-identical output. Policies (ported from v1's
        compose): out-of-phase skills dropped at skill level, head-reserve
        (first fragment of each skill always included), soft cap of 15
        fragments / 600 words dropping example→rationale first, system-class
        skills rendered first, provenance footer.
        """
        wanted_types = {t for t in (types or []) if t in FRAGMENT_TYPES}
        dropped: list[str] = []

        # Resolve in the order the LFM gave (its relevance ranking), dedup.
        selected: list[Skill] = []
        seen: set[str] = set()
        for key in skills:
            skill = self.skills.get(key)
            if skill is None:
                skill = next((s for s in self.skills.values() if s.name == key), None)
            if skill is None:
                dropped.append(f"{key} (unknown)")
            elif skill.id not in seen:
                seen.add(skill.id)
                selected.append(skill)

        # Phase scope: v1 fragments carry no per-fragment phase, so the
        # filter applies at skill level.
        if phase:
            in_phase: list[Skill] = []
            for skill in selected:
                if skill.phases and phase not in skill.phases:
                    dropped.append(f"{skill.id} (phase)")
                else:
                    in_phase.append(skill)
            selected = in_phase

        # System-class skills first (always-on), then the LFM's order.
        ordered = [s for s in selected if s.skill_class == "system"] + [
            s for s in selected if s.skill_class != "system"
        ]

        # Per-skill fragments: head (lowest sequence) always in; the rest
        # filtered by the selected types.
        per_skill: list[tuple[Skill, list[SkillFragment]]] = []
        for skill in ordered:
            frags = list(skill.fragments)
            if not frags and skill.body:
                frags = [SkillFragment(f"{skill.id}-f0", "execution", 0, skill.body)]
            frags.sort(key=lambda f: f.sequence)
            head_seq = frags[0].sequence
            head = [f for f in frags if f.sequence == head_seq]
            body = [f for f in frags if f.sequence != head_seq]
            if wanted_types:
                body = [f for f in body if f.fragment_type in wanted_types]
            per_skill.append((skill, head + body))

        # Soft cap: drop example→rationale→setup→verification→guardrail→
        # execution fragments (never heads) until within budget.
        drop_rank = {
            "example": 0,
            "rationale": 1,
            "setup": 2,
            "verification": 3,
            "guardrail": 4,
            "execution": 5,
        }

        def _render(frags_per_skill: list[tuple[Skill, list[SkillFragment]]]) -> str:
            kept_now = [(s, fs) for s, fs in frags_per_skill if fs]
            if not kept_now:
                return ""
            sections: list[str] = []
            for skill, frags in kept_now:
                block = [f"## skill: {skill.id}"]
                last_type: str | None = None
                for frag in frags:
                    if frag.fragment_type != last_type:
                        block.append(f"### {frag.fragment_type}")
                        last_type = frag.fragment_type
                    block.append(frag.content.strip())
                sections.append("\n\n".join(block))
            header = f"# Dynamic Skill ({phase})" if phase else "# Dynamic Skill"
            n_frags = sum(len(fs) for _, fs in kept_now)
            provenance = f"Provenance: {n_frags} fragments from {len(kept_now)} skills"
            return header + "\n\n" + "\n\n".join(sections) + "\n\n" + provenance

        # Budget on the rendered text (what the proxy injects) — header and
        # provenance words count against the 600-word cap, not just fragments.
        while True:
            text = _render(per_skill)
            n_frags = sum(len(fs) for _, fs in per_skill)
            if n_frags <= 15 and len(text.split()) <= 600:
                break
            head_ids = {id(f) for _, fs in per_skill for f in fs if f.sequence == fs[0].sequence}
            droppable = sorted(
                (f for _, fs in per_skill for f in fs if id(f) not in head_ids),
                key=lambda f: (drop_rank[f.fragment_type], f.sequence),
            )
            if not droppable:
                break
            frag = droppable[0]
            for _s, fs in per_skill:
                if frag in fs:
                    fs.remove(frag)
                    dropped.append(f"{frag.fragment_id} (cap)")
                    break

        # Skills left with no fragments contribute nothing — drop them.
        kept = [(s, fs) for s, fs in per_skill if fs]
        kept_ids = {s.id for s, _ in kept}
        for skill in ordered:
            if skill.id not in kept_ids:
                dropped.append(f"{skill.id} (no fragments)")
        per_skill = kept

        fragment_ids = [f.fragment_id for _, fs in per_skill for f in fs]
        source_skills = [s.id for s, _ in per_skill]
        if not per_skill:
            return {
                "skill": "",
                "fragments": [],
                "source_skills": [],
                "word_count": 0,
                "dropped": dropped,
            }

        text = _render(per_skill)
        return {
            "skill": text,
            "fragments": fragment_ids,
            "source_skills": source_skills,
            "word_count": len(text.split()),
            "dropped": dropped,
        }
