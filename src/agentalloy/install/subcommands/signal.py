"""``agentalloy signal`` — signal-layer CLI.

Commands:
    agentalloy signal evaluate-phase   Run pre-filter + gate eval for active phase
    agentalloy signal evaluate-system  Find system skills matching applies_when for a tool
    agentalloy signal watch-contract   Validate contract and invoke compose
    agentalloy signal check            Diagnostics: dump current state
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from agentalloy.install.output import print_rich
from agentalloy.signals.skill_loader import (  # type: ignore[reportPrivateUsage]
    _build_predicate_context,
    _load_workflow_skill_for_phase,
    _read_phase,
    _write_phase_atomic,
    _write_telemetry,
)

try:
    from agentalloy.embed_provider import get_embed_client
except Exception:  # pragma: no cover
    get_embed_client = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# evaluate-phase
# ---------------------------------------------------------------------------


def _evaluate_phase(args: argparse.Namespace) -> int:
    project_root = Path.cwd()
    current_phase = _read_phase(project_root)

    # Read prompt text
    prompt_text: str | None = None
    prompt_file = getattr(args, "prompt_file", None)
    if prompt_file and prompt_file != "/dev/null":
        import contextlib

        with contextlib.suppress(OSError):
            prompt_text = Path(prompt_file).read_text(encoding="utf-8", errors="replace")

    if current_phase is None:
        print(json.dumps({"matched": False, "reason": "no phase file"}))
        return 0

    skill = _load_workflow_skill_for_phase(current_phase, project_root)
    if skill is None:
        print(
            json.dumps({"matched": False, "reason": f"no workflow skill for phase={current_phase}"})
        )
        return 0

    gate_spec: dict[str, Any] = skill.get("exit_gates") or {}
    signal_keywords: list[str] = list(skill.get("signal_keywords") or [])

    ctx = _build_predicate_context(
        project_root,
        phase=current_phase,
        prompt_text=prompt_text,
        tool_name=getattr(args, "tool", None),
        tool_path=getattr(args, "tool_path", None),
    )

    from agentalloy.signals.prefilter import check_prefilter

    match = check_prefilter(signal_keywords, gate_spec, ctx)

    if match is None:
        _write_telemetry(
            {
                "task": prompt_text or "",
                "phase": current_phase,
                "event_type": "phase_eval",
                "pre_filter_matched": None,
            }
        )
        print(json.dumps({"matched": False}))
        return 0

    # Gate evaluation — build embed client (soft-fail: None degrades semantic predicates to UNKNOWN)
    lm_client = None
    try:
        from agentalloy.config import get_settings

        cfg = get_settings()
        if get_embed_client is not None:
            lm_client = get_embed_client(cfg)
    except Exception:
        pass

    from agentalloy.signals.gates import decide_transition

    decision = decide_transition(
        current_phase=current_phase,
        gate_spec=gate_spec,
        ctx=ctx,
        lm_client=lm_client,
    )

    _write_telemetry(
        {
            "task": prompt_text or "",
            "phase": current_phase,
            "event_type": "phase_transition" if decision.should_transition else "phase_eval",
            "pre_filter_matched": match.name,
            "gates_met": [g.gate_name for g in decision.gates_met],
            "gates_unmet": [g.gate_name for g in decision.gates_unmet],
            "qwen_calls": decision.qwen_calls,
        }
    )

    if decision.should_transition and decision.to_phase:
        _write_phase_atomic(project_root, decision.to_phase)
        next_skill = _load_workflow_skill_for_phase(decision.to_phase, project_root)
        prose = (next_skill or {}).get("raw_prose", "")
        if prose:
            print(f"[agentalloy-workflow]\n{prose}\n[/agentalloy-workflow]")
        print(
            json.dumps(
                {
                    "transition": True,
                    "from": current_phase,
                    "to": decision.to_phase,
                }
            ),
            file=sys.stderr,
        )
    else:
        print(
            json.dumps(
                {
                    "transition": False,
                    "gates_unmet": [g.gate_name for g in decision.gates_unmet],
                }
            )
        )

    for advisory in decision.advisories:
        print(advisory)

    return 0


# ---------------------------------------------------------------------------
# evaluate-system
# ---------------------------------------------------------------------------


def _evaluate_system(args: argparse.Namespace) -> int:
    tool_name = getattr(args, "tool", "") or ""
    project_root = Path.cwd()
    current_phase = _read_phase(project_root)

    ctx = _build_predicate_context(
        project_root,
        phase=current_phase,
        tool_name=tool_name,
    )

    try:
        import duckdb

        from agentalloy.profiles import detect_profile, profile_datastore_path

        profile = detect_profile(cwd=Path.cwd())
        db_path = profile_datastore_path(profile.name if profile else "default")
        if not db_path.exists():
            return 0

        with duckdb.connect(str(db_path), read_only=True) as con:
            rows = con.execute(
                "SELECT skill_id, raw_prose, applies_when FROM profile_skills WHERE skill_class = 'system'"
            ).fetchall()

    except Exception:
        return 0

    import yaml as _yaml

    for skill_id, raw_prose, applies_when_raw in rows:
        if not applies_when_raw:
            continue
        try:
            gate_spec: dict[str, Any] = _yaml.safe_load(applies_when_raw) or {}
        except Exception:
            continue

        from agentalloy.signals.gates import evaluate_node
        from agentalloy.signals.predicates import PredicateResult as PredicateResult_

        qwen_calls: list[int] = [0]
        result, _ = evaluate_node(gate_spec, ctx, None, qwen_calls)
        if result == PredicateResult_.MET:
            print(f"[agentalloy-system:{skill_id}]\n{raw_prose}\n[/agentalloy-system]")
            _write_telemetry(
                {
                    "task": tool_name,
                    "phase": current_phase or "",
                    "event_type": "system_skill_applied",
                }
            )

    return 0


# ---------------------------------------------------------------------------
# watch-contract
# ---------------------------------------------------------------------------


def _watch_contract(args: argparse.Namespace) -> int:
    contract_path_str = getattr(args, "path", "") or ""
    if not contract_path_str:
        print(json.dumps({"error": "no --path provided"}), file=sys.stderr)
        return 0

    contract_path = Path(contract_path_str)
    try:
        from agentalloy.contracts import parse_contract, validate_contract

        contract = parse_contract(contract_path)
        issues = validate_contract(contract, Path.cwd())
        if issues:
            print(
                json.dumps({"warning": "contract validation issues", "issues": issues}),
                file=sys.stderr,
            )
            return 0
    except Exception as exc:
        print(json.dumps({"warning": str(exc)}), file=sys.stderr)
        return 0

    # Invoke compose
    import subprocess

    try:
        from agentalloy.install import state as install_state

        st = install_state.load_state()
        port = st.get("port", 47950)
        subprocess.run(
            [
                "agentalloy",
                "compose",
                "--contract",
                str(contract_path),
                "--inject",
                "--port",
                str(port),
            ],
            capture_output=False,
            timeout=30,
        )
    except Exception as exc:
        print(json.dumps({"warning": f"compose failed: {exc}"}), file=sys.stderr)

    _write_telemetry(
        {
            "task": contract_path_str,
            "phase": contract.phase if "contract" in dir() else "",
            "event_type": "contract_retrieval",
        }
    )
    return 0


# ---------------------------------------------------------------------------
# check (diagnostics)
# ---------------------------------------------------------------------------


def _check(args: argparse.Namespace) -> int:
    project_root = Path.cwd()
    current_phase = _read_phase(project_root)
    skill = (
        _load_workflow_skill_for_phase(current_phase or "", project_root) if current_phase else None
    )

    report: dict[str, Any] = {
        "current_phase": current_phase,
        "active_workflow_skill": skill.get("skill_id") if skill else None,
        "signal_keywords": (skill or {}).get("signal_keywords", []),
        "exit_gates_keys": list((skill or {}).get("exit_gates", {}).keys()),
    }

    if getattr(args, "json_out", False):
        print(json.dumps(report, indent=2))
    else:
        print_rich("\n  [bold]Signal Report[/bold]\n")
        print_rich(f"  Phase: {current_phase or 'none'}")
        print_rich(f"  Workflow skill: {report['active_workflow_skill'] or 'none'}")
        print_rich(f"  Signal keywords: {', '.join(report['signal_keywords']) or 'none'}")
        print_rich(f"  Exit gates: {', '.join(report['exit_gates_keys']) or 'none'}")
        print_rich()
    return 0


# ---------------------------------------------------------------------------
# code-indexer-from-contract
# ---------------------------------------------------------------------------


def _code_indexer_from_contract(args: argparse.Namespace) -> int:
    contract_path_str = getattr(args, "path", "") or ""
    if not contract_path_str:
        return 0

    try:
        import urllib.parse
        import urllib.request

        from agentalloy.config import get_settings
        from agentalloy.contracts import code_indexer_query_params, parse_contract

        contract = parse_contract(Path(contract_path_str))
        params = code_indexer_query_params(contract, Path.cwd())
        ci_url = get_settings().code_indexer_url

        results: list[str] = []

        def _fetch(url: str) -> str | None:
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    return resp.read().decode("utf-8", errors="replace")
            except Exception:
                return None

        # Semantic search
        q_semantic = urllib.parse.urlencode(
            {"q": params.semantic_q, "repo": params.repo, "top_k": 5}
        )
        body = _fetch(f"{ci_url}/search/semantic?{q_semantic}")
        if body:
            results.append(f"[code-indexer:semantic]\n{body}\n[/code-indexer:semantic]")

        # Lexical search
        if params.lexical_q:
            q_lexical = urllib.parse.urlencode(
                {"q": params.lexical_q, "repo": params.repo, "top_k": 5}
            )
            body = _fetch(f"{ci_url}/search/lexical?{q_lexical}")
            if body:
                results.append(f"[code-indexer:lexical]\n{body}\n[/code-indexer:lexical]")

        if results:
            print("\n".join(results))

        _write_telemetry(
            {
                "task": params.semantic_q,
                "phase": contract.phase,
                "event_type": "contract_retrieval",
            }
        )
    except Exception as exc:
        print(json.dumps({"warning": f"code-indexer-from-contract: {exc}"}), file=sys.stderr)

    return 0


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "signal", help="Signal-layer: phase gate evaluation and system skill routing"
    )
    sub: argparse._SubParsersAction[argparse.ArgumentParser] = p.add_subparsers(dest="signal_cmd")  # pyright: ignore[reportPrivateUsage]

    ep: argparse.ArgumentParser = sub.add_parser(
        "evaluate-phase", help="Run pre-filter + gate evaluation for the active phase"
    )
    ep.add_argument("--prompt-file", dest="prompt_file", default=None)
    ep.add_argument("--tool", default=None)
    ep.add_argument("--tool-path", dest="tool_path", default=None)

    es: argparse.ArgumentParser = sub.add_parser(
        "evaluate-system", help="Emit system skill prose for matching applies_when"
    )
    es.add_argument("--tool", default="", help="Tool name being used (tool-use predicate context)")

    wc: argparse.ArgumentParser = sub.add_parser(
        "watch-contract", help="Validate contract and invoke compose"
    )
    wc.add_argument("--path", required=True, help="Path to the contract file")

    ci: argparse.ArgumentParser = sub.add_parser(
        "code-indexer-from-contract", help="Query code-indexer using contract params"
    )
    ci.add_argument("--path", required=True, help="Path to the contract file")

    ck: argparse.ArgumentParser = sub.add_parser(
        "check", help="Diagnostics: dump current signal state"
    )
    ck.add_argument("--json", dest="json_out", action="store_true", default=False)

    p.set_defaults(func=_dispatch)


def _dispatch(args: argparse.Namespace) -> int:
    cmd = getattr(args, "signal_cmd", None)
    if cmd == "evaluate-phase":
        return _evaluate_phase(args)
    if cmd == "evaluate-system":
        return _evaluate_system(args)
    if cmd == "watch-contract":
        return _watch_contract(args)
    if cmd == "code-indexer-from-contract":
        return _code_indexer_from_contract(args)
    if cmd == "check":
        return _check(args)
    print(
        "Usage: agentalloy signal {evaluate-phase,evaluate-system,watch-contract,check}",
        file=sys.stderr,
    )
    return 1
