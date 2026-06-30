# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
"""``verify-pack`` subcommand — post-ingest retrievability probe.

For each skill in a pack, runs:
  * **name probe**: canonical_name phrased as a task (easy — should always hit
    if the skill is indexed at all).
  * **topic probe**: first sentences of raw_prose with all name/id tokens
    removed (realistic — catches ranking weakness).

Both probes are run in-process against the local skill store + fragments
store. No HTTP service needs to be running, but the reembed pass must have
been run first (the fragments store must have embeddings for the pack's
skills).

Exit codes:
  0  all skills found (hit@k for every probe)
  1  one or more skills are unfindable
  2  usage / setup error (e.g. embeddings missing — run reembed first)
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml as _yaml

from agentalloy.install.output import add_json_flag, write_result

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Probe helpers  (mirrored from eval/retrieval_audit.py — kept inline so
# verify-pack has no dependency on the eval/ package)
# ---------------------------------------------------------------------------


def _strip_parenthetical(text: str) -> str:
    return re.sub(r"\s*[\(—–-].*$", "", text).strip()


def _name_probe(canonical_name: str) -> str:
    return f"How should I approach: {_strip_parenthetical(canonical_name)}?"


def _topic_probe(skill_id: str, canonical_name: str, raw_prose: str) -> str | None:
    """First sentences of prose with all name/id tokens removed."""
    body = re.sub(r"^#.*$", "", raw_prose, flags=re.MULTILINE)
    body = " ".join(body.split())
    sentences = re.split(r"(?<=[.!?]) ", body)
    snippet = " ".join(sentences[:2]).strip()
    if len(snippet) < 40:
        return None
    ban = {
        tok.lower()
        for tok in re.split(r"[\s\-_/()]+", f"{skill_id} {canonical_name}")
        if len(tok) > 2
    }
    kept = [w for w in snippet.split() if re.sub(r"\W", "", w).lower() not in ban]
    probe = " ".join(kept)
    return probe if len(probe) >= 40 else None


# ---------------------------------------------------------------------------
# Core probe logic
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    skill_id: str
    name_hit: bool = False
    topic_hit: bool | None = None  # None when no topic probe could be generated
    error: str = ""


@dataclass
class VerifyPackReport:
    pack_id: str
    k: int
    probes: list[ProbeResult] = field(default_factory=list)
    missing_embeddings: bool = False
    error: str = ""

    @property
    def all_hit(self) -> bool:
        return (
            not self.missing_embeddings
            and not self.error
            and all(p.name_hit and (p.topic_hit is not False) for p in self.probes if not p.error)
        )

    @property
    def any_unfindable(self) -> bool:
        return any(not p.name_hit or p.topic_hit is False for p in self.probes if not p.error)


def _load_pack_skills(pack_dir: Path) -> list[dict[str, Any]]:
    """Load skill metadata from pack.yaml + YAML files. Returns list of dicts."""
    manifest_path = pack_dir / "pack.yaml"
    if not manifest_path.is_file():
        return []
    try:
        manifest: Any = _yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except _yaml.YAMLError:
        return []
    skills_entries: list[dict[str, Any]] = manifest.get("skills") or []
    result: list[dict[str, Any]] = []
    for entry in skills_entries:
        fname = str(entry.get("file", ""))
        yaml_path = pack_dir / fname
        if not yaml_path.is_file():
            continue
        try:
            data: Any = _yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        except _yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        result.append(
            {
                "skill_id": str(data.get("skill_id") or ""),
                "canonical_name": str(data.get("canonical_name") or ""),
                "raw_prose": str(data.get("raw_prose") or ""),
                "phase_scope": data.get("phase_scope") or ["build"],
                "skill_class": str(data.get("skill_class") or "domain"),
                "deprecated": bool(data.get("deprecated", False)),
            }
        )
    return result


def _fragment_ids_for_skill(skill_id: str, num_fragments: int) -> list[str]:
    """Generate the expected fragment IDs for a skill (v1 only)."""
    return [f"{skill_id}-v1-f{i}" for i in range(1, num_fragments + 1)]


def probe_pack(pack_dir: Path, k: int = 4) -> VerifyPackReport:
    """Run name + topic probes for every skill in *pack_dir*.

    Returns a :class:`VerifyPackReport`. Callers should check
    ``.missing_embeddings`` and ``.error`` before interpreting ``.probes``.
    """
    from agentalloy.config import get_settings  # noqa: PLC0415
    from agentalloy.lm_client import LMUnavailable  # noqa: PLC0415
    from agentalloy.retrieval.domain import retrieve_domain_candidates  # noqa: PLC0415
    from agentalloy.storage.open import open_fragments, open_skills  # noqa: PLC0415

    manifest_path = pack_dir / "pack.yaml"
    if not manifest_path.is_file():
        return VerifyPackReport(pack_id=pack_dir.name, k=k, error=f"No pack.yaml in {pack_dir}")

    skills = _load_pack_skills(pack_dir)
    if not skills:
        return VerifyPackReport(pack_id=pack_dir.name, k=k, error="No skills found in pack.")

    settings = get_settings()
    report = VerifyPackReport(pack_id=pack_dir.name, k=k)

    # --- Check embeddings presence ---
    vs = open_fragments(settings)
    try:
        total = vs.count_embeddings()
    finally:
        vs.close()
    if total == 0:
        report.missing_embeddings = True
        report.error = (
            "Fragments store has 0 embeddings. "
            "Run `agentalloy reembed` first, then re-run verify-pack."
        )
        return report

    # --- Set up stores for in-process retrieval ---
    try:
        store = open_skills(settings, read_only=True)
    except Exception as exc:
        report.error = f"Cannot open skill store: {exc}"
        return report

    try:
        # Build a minimal EmbedClient from the configured embed server.
        from agentalloy.embed_provider import get_embed_client  # noqa: PLC0415

        lm = get_embed_client(settings)

        vs = open_fragments(settings)
        try:
            for skill in skills:
                if skill["deprecated"]:
                    continue
                sid = skill["skill_id"]
                if not sid:
                    continue
                name = skill["canonical_name"] or sid
                prose = skill["raw_prose"]
                phase_list: list[str] = skill["phase_scope"] or ["build"]
                # Use the first non-system phase as our probe phase.
                _VALID_PROBE_PHASES = frozenset({"intake", "spec", "design", "build", "qa", "ship"})
                probe_phase = next((p for p in phase_list if p in _VALID_PROBE_PHASES), "build")

                pr = ProbeResult(skill_id=sid)
                probes: dict[str, str] = {"name": _name_probe(name)}
                topic = _topic_probe(sid, name, prose)
                if topic:
                    probes["topic"] = topic

                for probe_type, query in probes.items():
                    try:
                        result = retrieve_domain_candidates(
                            store,
                            lm,
                            vs,
                            task=query,
                            phase=probe_phase,  # type: ignore[arg-type]
                            domain_tags=None,
                            k=k,
                            embedding_model=settings.runtime_embedding_model,
                        )
                        from agentalloy.retrieval.embedding_errors import (  # noqa: PLC0415
                            EmbeddingErrorResult,
                        )

                        if isinstance(result, EmbeddingErrorResult):
                            pr.error = f"Embedding error: {result}"
                            break
                        retrieved_ids = [c.skill_id for c in result.candidates]
                        hit = sid in retrieved_ids
                        if probe_type == "name":
                            pr.name_hit = hit
                        else:
                            pr.topic_hit = hit
                    except LMUnavailable as exc:
                        pr.error = (
                            f"Embed server unavailable: {exc}. Start the embed server and retry."
                        )
                        break

                report.probes.append(pr)
        finally:
            vs.close()
    finally:
        store.close()

    return report


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def _render_human(result: dict[str, Any]) -> None:
    from agentalloy.install.output import print_rich  # noqa: PLC0415

    pack_id = result.get("pack_id", "unknown")
    k = result.get("k", 4)
    print_rich(f"\n  [bold]verify-pack: {pack_id}[/bold]  (hit@{k})\n")

    if result.get("missing_embeddings") or result.get("error"):
        msg = result.get("error") or "missing embeddings"
        print_rich(f"  [red]ERROR[/red]: {msg}\n")
        return

    probes = result.get("probes") or []
    passed = 0
    failed = 0
    for p in probes:
        sid = p.get("skill_id", "?")
        name_hit = p.get("name_hit", False)
        topic_hit = p.get("topic_hit")
        err = p.get("error", "")
        if err:
            print_rich(f"  [yellow]SKIP[/yellow]  {sid}: {err}")
            continue
        name_ok = "✓" if name_hit else "✗"
        if topic_hit is None:
            topic_ok = "–"
        elif topic_hit:
            topic_ok = "✓"
        else:
            topic_ok = "✗"
        row_ok = name_hit and (topic_hit is not False)
        if row_ok:
            passed += 1
            print_rich(f"  [green]PASS[/green]  {sid}  name={name_ok}  topic={topic_ok}")
        else:
            failed += 1
            print_rich(f"  [red]FAIL[/red]  {sid}  name={name_ok}  topic={topic_ok}")

    print_rich(f"\n  Passed: {passed}  Failed: {failed}  Total: {len(probes)}\n")


def _run(args: argparse.Namespace) -> int:
    pack_path = Path(args.pack_path)
    if not pack_path.is_dir():
        print(f"error: not a directory: {pack_path}", file=sys.stderr)
        return 2
    if not (pack_path / "pack.yaml").is_file():
        print(f"error: no pack.yaml in {pack_path}", file=sys.stderr)
        return 2

    k: int = args.k
    report = probe_pack(pack_path, k=k)

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "action": "verified"
        if report.all_hit
        else ("unreachable" if report.any_unfindable else "error"),
        "pack_id": report.pack_id,
        "k": report.k,
        "missing_embeddings": report.missing_embeddings,
        "error": report.error,
        "probes": [
            {
                "skill_id": p.skill_id,
                "name_hit": p.name_hit,
                "topic_hit": p.topic_hit,
                "error": p.error,
            }
            for p in report.probes
        ],
        "all_hit": report.all_hit,
        "any_unfindable": report.any_unfindable,
    }

    write_result(result, args, human_fn=_render_human)

    if report.missing_embeddings or report.error:
        return 2
    if report.any_unfindable:
        return 1
    return 0


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    p = subparsers.add_parser(
        "verify-pack",
        help=(
            "Run name + topic probes for each skill in a pack against local "
            "stores (no HTTP service required). Non-zero exit if any skill is "
            "unfindable. Requires reembed to have been run first."
        ),
    )
    p.add_argument(
        "pack_path",
        help="Path to the local pack directory containing pack.yaml.",
    )
    p.add_argument(
        "--k",
        type=int,
        default=4,
        help="Probe hit@k cutoff (default 4).",
    )
    add_json_flag(p)
    p.set_defaults(func=_run)
