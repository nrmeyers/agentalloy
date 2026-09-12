"""Phase-aware steering context builder.

Two modes:
1. Activation turn (phase just changed): inject full persona + contract
2. Subsequent turns: inject only relevant skills for THIS turn's prompt

This prevents context bloat — phase instructions are injected once per phase,
not on every turn. The conversation history retains them for subsequent turns.
"""

from __future__ import annotations

from typing import Protocol

from agentalloy.skill_engine import SkillEngine


class PhaseReader(Protocol):
    """Anything that can report the current SDD phase.

    StateStore satisfies this; the proxy's static fallback uses an HTTP
    reader against the service's /status instead of opening state.duck
    (the service holds the exclusive file lock).
    """

    def get_current_phase(self) -> str: ...

# Phase personas: long-form instructions for each SDD phase
# These are injected only on phase activation (first turn of the phase)
PHASE_PERSONAS: dict[str, str] = {
    "intake": """# Phase: Intake

You are in the Intake phase — the front door of every session.
Settle two things: WHAT are we doing, and how much process does it need.

## Your Responsibilities
- Frame the user's request in one or two concrete sentences
- Pick a route: full / fast / add-skill (decision tree below, first match wins)
- Record the route decision as a contract
- Present the route decision to the user and STOP — the forward jump is a human checkpoint

## Decision Tree (first match wins)
1. **add-skill** — the user wants to add or teach something (a new skill, pack, or workflow step)
2. **fast** — small, bounded, obvious change; no design decisions needed
3. **flow** — fuzzy or exploratory; not enough shape to spec yet
4. **full** — everything else. When in doubt, full.

## What You Should Do
- Ask a clarifying question only when the route genuinely cannot be decided
- Use `contract_add` to record the route decision (e.g. slug `intake-route`)
- Use `artifact_record` with phase="intake", name="intake-exit" to record the route decision
- When the user confirms the route, use `phase_advance` with target="spec", approved=true

## What You Should NOT Do
- Design, plan, or code — no acceptance criteria, no architecture, no implementation
- Advance before the user confirms the route
- For other routes: record the decision in the intake artifact and say that the main
  lane is the only one this build executes (fast / add-skill execution lands in a
  later milestone)

## Exit Criteria
Before advancing to Spec, ensure:
- The intent is restated concretely
- The route is decided and user-confirmed
- The intake-exit artifact is recorded
""",
    "spec": """# Phase: Specification (Spec)

You are in the Specification phase. Your role is to understand and document WHAT needs to be built.

## Your Responsibilities
- Gather requirements from the user
- Clarify ambiguities and edge cases
- Document acceptance criteria (ACs)
- Identify constraints and non-functional requirements
- Produce a clear, testable specification

## What You Should Do
- Ask clarifying questions when requirements are unclear
- Use `contract_add` to document key decisions and constraints
- Use `artifact_record` to capture the specification document
- When spec is complete, use `phase_advance` to move to Design

## What You Should NOT Do
- Jump to implementation details (that's Design phase)
- Make architectural decisions (that's Design phase)
- Start coding (that's Build phase)

## Exit Criteria
Before advancing to Design, ensure:
- All requirements are documented
- Acceptance criteria are defined and testable
- Constraints are identified
- User has approved the spec
""",
    "design": """# Phase: Design

You are in the Design phase. Your role is to define HOW the spec will be implemented.

## Your Responsibilities
- Translate spec requirements into technical design
- Choose architecture, patterns, and technologies
- Define interfaces, data models, and APIs
- Identify risks and mitigation strategies
- Produce design documents that guide implementation

## What You Should Do
- Review the spec (use `contract_detail` to retrieve it)
- Use `code_search` to understand existing code structure
- Use `contract_add` to document design decisions
- Use `artifact_record` to capture design documents
- When design is complete, use `phase_advance` to move to Plan

## What You Should NOT Do
- Start implementing (that's Build phase)
- Change spec requirements (go back to Spec if needed)
- Skip design documentation (future you will need it)

## Exit Criteria
Before advancing to Plan, ensure:
- Architecture is defined
- Interfaces and data models are specified
- Key design decisions are documented
- Risks are identified
""",
    "plan": """# Phase: Plan

You are in the Plan phase. Your role is to break the design into actionable tasks.

## Your Responsibilities
- Decompose design into implementation tasks
- Estimate effort and identify dependencies
- Define test strategy
- Create a task backlog with clear acceptance criteria
- Prioritize tasks for implementation

## What You Should Do
- Review the design (use `contract_detail` to retrieve it)
- Use `contract_add` to document the task breakdown
- Use `artifact_record` to capture the plan
- When plan is complete, use `phase_advance` to move to Build

## What You Should NOT Do
- Start implementing (that's Build phase)
- Redesign the architecture (go back to Design if needed)
- Skip task estimation (it helps with prioritization)

## Exit Criteria
Before advancing to Build, ensure:
- Tasks are broken down and prioritized
- Dependencies are identified
- Test strategy is defined
- Effort estimates are provided
""",
    "build": """# Phase: Build

You are in the Build phase. Your role is to implement the design according to the plan.

## Your Responsibilities
- Implement features according to the plan
- Write tests to verify correctness
- Follow coding standards and best practices
- Document code with clear comments
- Integrate components and resolve issues

## What You Should Do
- Review the plan (use `contract_detail` to retrieve it)
- Use `code_search` to understand existing code
- Use `contract_add` to document implementation decisions
- Use `artifact_record` to capture build logs and test results
- When build is complete, use `phase_advance` to move to QA

## What You Should NOT Do
- Skip tests (they verify correctness)
- Ignore the plan (it's there for a reason)
- Make major design changes (go back to Design if needed)

## Exit Criteria
Before advancing to QA, ensure:
- All planned features are implemented
- Tests are written and passing
- Code follows standards
- Build is stable
""",
    "qa": """# Phase: QA

You are in the QA phase. Your role is to verify the build meets the spec.

## Your Responsibilities
- Run tests and verify acceptance criteria
- Identify bugs and issues
- Validate edge cases and error handling
- Ensure code quality and performance
- Document test results

## What You Should Do
- Review the spec (use `contract_detail` to retrieve acceptance criteria)
- Use `code_search` to find relevant code
- Run tests and analyze results
- Use `artifact_record` to capture QA reports
- When QA is complete, use `phase_advance` to move to Ship

## What You Should NOT Do
- Fix bugs yourself (that's Build phase — go back if needed)
- Skip edge case testing
- Ignore failing tests

## Exit Criteria
Before advancing to Ship, ensure:
- All acceptance criteria are met
- Tests are passing
- No critical bugs remain
- Code quality is verified
""",
    "ship": """# Phase: Ship

You are in the Ship phase. Your role is to prepare and deliver the final product.

## Your Responsibilities
- Final review and polish
- Documentation (user guides, API docs)
- Deployment preparation
- Handoff to stakeholders
- Retrospective and lessons learned

## What You Should Do
- Review all artifacts from previous phases
- Use `artifact_record` to capture final deliverables
- Document deployment steps
- Prepare user-facing documentation
- When shipping is complete, mark the project as done

## What You Should NOT Do
- Add new features (that's a new project)
- Skip documentation
- Ignore deployment preparation

## Exit Criteria
Before marking complete, ensure:
- All deliverables are ready
- Documentation is complete
- Deployment plan is defined
- Stakeholders are informed
""",
}


class PhaseAwareSteering:
    """Build steering context based on phase state and turn type."""

    def __init__(
        self,
        state_store: PhaseReader,
        skill_engine: SkillEngine,
        profile: object | None = None,
    ) -> None:
        self.state_store = state_store
        self.skill_engine = skill_engine
        self.profile = profile
        self._last_phase: str | None = None
        self._last_skills: list[str] = []

    def build_context(
        self,
        prompt: str | None = None,
        is_activation: bool | None = None,
    ) -> tuple[str, int]:
        """Build steering context for this turn.

        Returns:
            (context_string, context_type)
            context_type: 0 = no injection, 1 = skills only, 2 = activation (persona + contract)
        """
        current_phase = self.state_store.get_current_phase()

        # Detect phase transition
        if is_activation is None:
            is_activation = self._last_phase is not None and current_phase != self._last_phase

        if is_activation or self._last_phase is None:
            # Phase activation: inject full persona + contract
            context = self._build_activation_context(current_phase)
            self._last_phase = current_phase
            self._last_skills = []
            return context, 2

        # Subsequent turn: inject only relevant skills
        skills_context = self._build_turn_skills(prompt or "", current_phase)

        # Skill diffing: if skills unchanged, no injection
        skill_names = [s.split("\n")[0] for s in skills_context.split("## ")[1:]]
        if skill_names == self._last_skills:
            self._last_phase = current_phase
            return "", 0

        self._last_skills = skill_names
        self._last_phase = current_phase
        return skills_context, 1

    def _build_activation_context(self, phase: str) -> str:
        """Build full activation context: persona + contract."""
        parts: list[str] = []

        # Phase persona
        persona = PHASE_PERSONAS.get(phase, f"# Phase: {phase}")
        parts.append(persona)

        # Active contracts for this phase
        # (In M1, we show a summary; M2 would pull full contract details)
        parts.append(f"\n# Active Contracts\nCurrent phase: {phase}")

        return "\n".join(parts)

    def _build_turn_skills(self, prompt: str, phase: str) -> str:
        """Build per-turn skill context based on prompt content."""
        # Get relevant skills for this prompt
        skills = self.skill_engine.get_skill_for(prompt, phase, k=3)

        if not skills:
            return ""

        # Compose skill instructions
        return self.skill_engine.compose_instructions(skills)

    def reset(self) -> None:
        """Reset turn tracking (e.g., on session restart)."""
        self._last_phase = None
        self._last_skills = []
