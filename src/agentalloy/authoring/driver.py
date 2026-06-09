"""Author step: SKILL.md → draft review YAML, guided by sys-skill-authoring.

For each discovered SKILL.md the driver loads the authoring-agent prose as
system prompt, hands the source to the Author LLM, and writes the returned
YAML to ``skill-source/pending-qa/<skill_id>.yaml``.

The author is expected to emit YAML matching ``ReviewRecord`` — see
``fixtures/skill-authoring-agent.md`` for the contract.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

from agentalloy.authoring.paths import PipelinePaths
from agentalloy.config import get_settings
from agentalloy.lm_client import LMClientError, OpenAICompatClient

logger = logging.getLogger(__name__)

_SKILL_MD_GLOB = "**/SKILL.md"
# Directories we never recurse into: anything under the staging tree itself,
# plus hidden dirs.
_EXCLUDED_DIRS = {
    "pending-qa",
    "pending-review",
    "pending-revision",
    "rejected",
    "needs-human",
    "node_modules",
    ".git",
}


@dataclass
class DraftResult:
    source: Path
    skill_id: str | None
    draft_path: Path | None
    error: str | None = None


def discover_skill_md(root: Path) -> Iterator[Path]:
    """Yield SKILL.md paths under ``root``, skipping staging + hidden dirs."""
    for path in root.glob(_SKILL_MD_GLOB):
        parts = set(path.parts)
        if parts & _EXCLUDED_DIRS:
            continue
        if any(p.startswith(".") for p in path.parts):
            continue
        yield path


def load_authoring_prompt(repo_root: Path) -> str:
    """Load the sys-skill-authoring fixture as the system prompt."""
    fixture = repo_root / "fixtures" / "skill-authoring-agent.md"
    if not fixture.exists():
        raise FileNotFoundError(
            f"authoring fixture not found at {fixture}; "
            "either restore the bootstrap fixture or bootstrap the agent skill first"
        )
    return fixture.read_text(encoding="utf-8")


def author_one(
    source: Path,
    *,
    client: OpenAICompatClient,
    model: str,
    system_prompt: str,
    paths: PipelinePaths,
) -> DraftResult:
    """Run the Author LLM on a single SKILL.md, emit a draft YAML."""
    try:
        source_text = source.read_text(encoding="utf-8")
    except OSError as exc:
        return DraftResult(source=source, skill_id=None, draft_path=None, error=f"read: {exc}")

    user_prompt = _build_user_prompt(source, source_text)

    try:
        raw = client.chat(
            model=model,
            system=system_prompt,
            user=user_prompt,
            temperature=0.2,
        )
    except LMClientError as exc:
        return DraftResult(source=source, skill_id=None, draft_path=None, error=f"llm: {exc}")

    yaml_text = _strip_code_fence(raw)
    try:
        data: Any = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return DraftResult(
            source=source, skill_id=None, draft_path=None, error=f"yaml-parse: {exc}"
        )

    if not isinstance(data, dict):
        return DraftResult(
            source=source,
            skill_id=None,
            draft_path=None,
            error="author returned no skill_id",
        )
    data_dict = cast(dict[str, Any], data)
    if not data_dict.get("skill_id"):
        return DraftResult(
            source=source,
            skill_id=None,
            draft_path=None,
            error="author returned no skill_id",
        )

    skill_id = str(data_dict["skill_id"]).strip()
    out_path = paths.pending_qa / f"{skill_id}.yaml"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml_text, encoding="utf-8")

    return DraftResult(source=source, skill_id=skill_id, draft_path=out_path)


def run_author(
    source_dir: Path,
    repo_root: Path,
    *,
    paths: PipelinePaths,
    client: OpenAICompatClient | None = None,
) -> list[DraftResult]:
    """Walk ``source_dir`` for SKILL.md files, author a draft for each."""
    settings = get_settings()
    ac = settings.require_authoring_config()
    owned_client = client is None
    _client = client or OpenAICompatClient(ac.lm_base_url)
    system_prompt = load_authoring_prompt(repo_root)
    paths.ensure_all()

    try:
        results: list[DraftResult] = []
        for source in discover_skill_md(source_dir):
            logger.info("authoring %s", source)
            result = author_one(
                source,
                client=_client,
                model=ac.model,
                system_prompt=system_prompt,
                paths=paths,
            )
            if result.error:
                logger.warning("  → error: %s", result.error)
            else:
                logger.info("  → wrote %s", result.draft_path)
            results.append(result)
        return results
    finally:
        if owned_client:
            _client.close()


def revise_one(
    draft_path: Path,
    *,
    client: OpenAICompatClient,
    model: str,
    system_prompt: str,
    paths: PipelinePaths,
) -> DraftResult:
    """Re-author a draft using the critic's feedback from the sibling .qa.md."""
    try:
        draft_text = draft_path.read_text(encoding="utf-8")
    except OSError as exc:
        return DraftResult(source=draft_path, skill_id=None, draft_path=None, error=f"read: {exc}")

    report_path = draft_path.with_suffix(".qa.md")
    critic_feedback = (
        report_path.read_text(encoding="utf-8")
        if report_path.exists()
        else "(no critic report found)"
    )

    try:
        parsed: Any = yaml.safe_load(draft_text) or {}
    except yaml.YAMLError as exc:
        return DraftResult(source=draft_path, skill_id=None, draft_path=None, error=f"yaml: {exc}")

    if not isinstance(parsed, dict):
        return DraftResult(
            source=draft_path,
            skill_id=None,
            draft_path=None,
            error="draft top-level is not a mapping",
        )
    parsed_dict = cast(dict[str, Any], parsed)

    source_text = str(parsed_dict.get("raw_prose", ""))
    skill_hint = str(parsed_dict.get("skill_id", draft_path.stem))

    user_prompt = _build_revise_prompt(skill_hint, source_text, draft_text, critic_feedback)

    try:
        raw = client.chat(
            model=model,
            system=system_prompt,
            user=user_prompt,
            temperature=0.2,
        )
    except LMClientError as exc:
        return DraftResult(source=draft_path, skill_id=None, draft_path=None, error=f"llm: {exc}")

    yaml_text = _strip_code_fence(raw)
    try:
        data: Any = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return DraftResult(
            source=draft_path, skill_id=None, draft_path=None, error=f"yaml-parse: {exc}"
        )

    if not isinstance(data, dict):
        return DraftResult(
            source=draft_path,
            skill_id=None,
            draft_path=None,
            error="revised draft missing skill_id",
        )
    data_dict = cast(dict[str, Any], data)
    if not data_dict.get("skill_id"):
        return DraftResult(
            source=draft_path,
            skill_id=None,
            draft_path=None,
            error="revised draft missing skill_id",
        )

    skill_id = str(data_dict["skill_id"]).strip()
    out_path = paths.pending_qa / f"{skill_id}.yaml"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml_text, encoding="utf-8")

    # Clean up the pending-revision sidecar now that it's been re-authored.
    draft_path.unlink(missing_ok=True)
    report_path.unlink(missing_ok=True)

    return DraftResult(source=draft_path, skill_id=skill_id, draft_path=out_path)


def run_revise(
    repo_root: Path,
    paths: PipelinePaths,
    *,
    client: OpenAICompatClient | None = None,
) -> list[DraftResult]:
    """Walk pending-revision/, re-author each draft using its critic feedback."""
    settings = get_settings()
    ac = settings.require_authoring_config()
    owned = client is None
    _client = client or OpenAICompatClient(ac.lm_base_url)
    system_prompt = load_authoring_prompt(repo_root)
    paths.ensure_all()

    try:
        results: list[DraftResult] = []
        for draft in sorted(paths.pending_revision.glob("*.yaml")):
            logger.info("revising %s", draft)
            result = revise_one(
                draft,
                client=_client,
                model=ac.model,
                system_prompt=system_prompt,
                paths=paths,
            )
            if result.error:
                logger.warning("  → error: %s", result.error)
            else:
                logger.info("  → wrote %s", result.draft_path)
            results.append(result)
        return results
    finally:
        if owned:
            _client.close()


def _build_revise_prompt(
    skill_hint: str, source_text: str, previous_draft: str, critic_feedback: str
) -> str:
    return (
        f"Your previous draft for skill_id `{skill_hint}` was sent to the QA "
        f"Critic and rejected with revise feedback. Produce a CORRECTED review "
        f"YAML that addresses the blocking issues. Emit YAML only, no prose, "
        f"no code fences.\n\n"
        f"---ORIGINAL SOURCE PROSE---\n{source_text}\n---END SOURCE---\n\n"
        f"---YOUR PREVIOUS DRAFT---\n{previous_draft}\n---END PREVIOUS DRAFT---\n\n"
        f"---CRITIC FEEDBACK---\n{critic_feedback}\n---END FEEDBACK---\n\n"
        f"Apply the critic's corrections. Keep what was correct. Do not "
        f"rewrite the operator's source prose — only restructure fragments.\n\n"
        f"/no_think"
    )


def _build_user_prompt(source: Path, source_text: str) -> str:
    # ``/no_think`` disables the Qwen3 reasoning phase. Authoring is a
    # transformation task (structured YAML emission) with no genuine need for
    # chain-of-thought — with thinking on, the model burns its full token
    # budget refining the draft in reasoning_content and emits nothing.
    return (
        f"Source file: {source}\n"
        f"Produce the review YAML for this skill per the Skill Authoring Agent "
        f"contract. Emit YAML only, no prose, no code fences.\n\n"
        f"---SOURCE---\n{source_text}\n---END SOURCE---\n\n"
        f"/no_think"
    )


_FENCE_RE = re.compile(r"^\s*```(?:ya?ml)?\s*\n(.*?)\n```\s*$", re.DOTALL)
_OPEN_FENCE_RE = re.compile(r"^\s*```(?:ya?ml)?\s*\n", re.IGNORECASE)
_CLOSE_FENCE_RE = re.compile(r"\n```\s*$")


def _strip_code_fence(text: str) -> str:
    """Tolerate an LLM that wraps YAML in a ```yaml ... ``` fence.

    Strips opening and closing fences independently — some models emit only
    one or the other, or trail content after the closing fence.
    """
    s = text.strip()
    m = _FENCE_RE.match(s)
    if m:
        return m.group(1)
    s = _OPEN_FENCE_RE.sub("", s, count=1)
    s = _CLOSE_FENCE_RE.sub("", s)
    return s.strip()
