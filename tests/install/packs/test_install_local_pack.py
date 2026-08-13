# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownLambdaType=false
"""Unit tests for the local-pack install path (`install_local_pack`).

Covers the manifest validator, fragment-count drift detection,
embedding-dim hard-block, duplicate-skill outcome classification, and
the dependency picker's missing-dep warning.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from agentalloy.install.subcommands import install_pack as ip
from agentalloy.install.subcommands import install_packs as ips

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Fragment content templates that are simultaneously:
#   - >= _FRAG_WORDS_WARN_MIN (25) words, so `_lint` doesn't flag them as
#     under-discriminative for nomic-embed-text-v1.5 (folded into hard errors
#     under --strict, the new install_local_pack default);
#   - reused verbatim in `raw_prose` below, so `_lint`'s content-drift check
#     ("fragment content is not a contiguous slice of raw_prose") never fires.
_LINT_CLEAN_FRAGMENT_TEMPLATES: dict[str, str] = {
    "execution": (
        "Run the {skill_id} workflow end to end by gathering every required input "
        "value, invoking the primary command with those inputs, waiting for it to "
        "finish, and confirming the operation completed without raising any errors "
        "before moving on to the next stage of the task."
    ),
    "verification": (
        "After completing the {skill_id} steps, verify the outcome by checking that "
        "the expected artifacts exist on disk, the logs show no unexpected errors, "
        "and any downstream consumer can read the produced output without further "
        "manual intervention."
    ),
    "rationale": (
        "This approach is recommended for {skill_id} because it keeps the workflow "
        "predictable and auditable, reduces the chance of a silent failure going "
        "unnoticed, and matches the conventions already established elsewhere in "
        "the corpus for comparable domain skills."
    ),
    "example": (
        "For example, a typical {skill_id} invocation supplies a small, realistic "
        "input, runs the command exactly as documented, and inspects the resulting "
        "output to confirm it matches the documented shape before trusting it in a "
        "larger automated pipeline."
    ),
}

# execution first (hard-required by `_validate`), then verification and
# rationale (both required by `_lint` under --strict) — anything beyond
# index 2 cycles back through `example` so larger fixtures stay lint-clean.
_LINT_CLEAN_TYPE_ORDER = ["execution", "verification", "rationale", "example"]


def _write_skill_yaml(
    pack_dir: Path,
    skill_id: str,
    *,
    fragments: int = 3,
    canonical_name: str | None = None,
) -> Path:
    """Write a lint-clean domain-skill YAML (passes `ingest._lint` under --strict).

    Lint-clean requires >= 3 fragments: `execution` is hard-required by
    `_validate`, and `_lint` (under --strict, the new install_local_pack
    default) additionally requires a `rationale` and a `verification`
    fragment — with only 1-2 fragments, at least one of those is
    structurally impossible to include, so `fragments < 3` will not pass a
    strict Gate 1. Callers that never reach a strict lint gate (e.g. the
    `_read_pack_manifest` drift-detection tests) may still use
    `fragments < 3`.
    """
    frag_types = [_LINT_CLEAN_TYPE_ORDER[i % len(_LINT_CLEAN_TYPE_ORDER)] for i in range(fragments)]
    frag_contents = [
        _LINT_CLEAN_FRAGMENT_TEMPLATES[t].format(skill_id=skill_id) for t in frag_types
    ]
    fy = [
        {"sequence": i + 1, "fragment_type": t, "content": c}
        for i, (t, c) in enumerate(zip(frag_types, frag_contents, strict=True))
    ]
    doc = {
        "skill_id": skill_id,
        "canonical_name": canonical_name or skill_id.replace("-", " ").title(),
        "category": "engineering",
        "skill_class": "domain",
        "domain_tags": ["test"],
        "always_apply": False,
        "phase_scope": None,
        "category_scope": None,
        "author": "test",
        "change_summary": "test fixture",
        "raw_prose": f"# {skill_id}\n\n" + "\n\n".join(frag_contents),
        "fragments": fy,
    }
    path = pack_dir / f"{skill_id}.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _write_pack_manifest(
    pack_dir: Path,
    name: str,
    skills: list[dict[str, Any]],
    *,
    embed_model: str = "nomic-embed-text-v1.5.Q8_0.gguf",
    embedding_dim: int = 768,
    extra: dict[str, Any] | None = None,
) -> Path:
    manifest = {
        "name": name,
        "version": "1.0.0",
        "tier": "tooling",
        "description": f"{name} test pack",
        "author": "test",
        "embed_model": embed_model,
        "embedding_dim": embedding_dim,
        "license": "MIT",
        "homepage": "https://example.com",
        "depends_on": [],
        "skills": skills,
    }
    if extra:
        manifest.update(extra)
    path = pack_dir / "pack.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _read_pack_manifest — schema + drift detection
# ---------------------------------------------------------------------------


class TestPackManifestValidation:
    def test_missing_pack_yaml(self, tmp_path: Path) -> None:
        manifest, errors = ip._read_pack_manifest(tmp_path)  # pyright: ignore[reportPrivateUsage]
        assert manifest is None
        assert any("missing pack.yaml" in e for e in errors)

    def test_missing_required_field(self, tmp_path: Path) -> None:
        # Manifest lacks `embed_model`
        (tmp_path / "pack.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "x",
                    "version": "1.0.0",
                    "embedding_dim": 768,
                    "skills": [],
                }
            )
        )
        manifest, errors = ip._read_pack_manifest(tmp_path)  # pyright: ignore[reportPrivateUsage]
        assert manifest is not None
        assert any("missing required field: embed_model" in e for e in errors)

    def test_skill_file_not_on_disk(self, tmp_path: Path) -> None:
        _write_pack_manifest(
            tmp_path,
            "x",
            [
                {"skill_id": "ghost", "file": "ghost.yaml", "fragment_count": 1},
            ],
        )
        manifest, errors = ip._read_pack_manifest(tmp_path)  # pyright: ignore[reportPrivateUsage]
        assert manifest is not None
        assert any("file not found on disk" in e for e in errors)

    def test_fragment_count_drift_caught(self, tmp_path: Path) -> None:
        _write_skill_yaml(tmp_path, "real-skill", fragments=3)
        _write_pack_manifest(
            tmp_path,
            "x",
            [
                {"skill_id": "real-skill", "file": "real-skill.yaml", "fragment_count": 99},
            ],
        )
        manifest, errors = ip._read_pack_manifest(tmp_path)  # pyright: ignore[reportPrivateUsage]
        assert manifest is not None
        assert any("fragment_count drift" in e for e in errors)

    def test_skill_id_drift_caught(self, tmp_path: Path) -> None:
        _write_skill_yaml(tmp_path, "real-skill", fragments=2)
        _write_pack_manifest(
            tmp_path,
            "x",
            [
                {"skill_id": "wrong-id", "file": "real-skill.yaml", "fragment_count": 2},
            ],
        )
        manifest, errors = ip._read_pack_manifest(tmp_path)  # pyright: ignore[reportPrivateUsage]
        assert manifest is not None
        assert any("skill_id drift" in e for e in errors)

    def test_valid_manifest_parses_clean(self, tmp_path: Path) -> None:
        _write_skill_yaml(tmp_path, "good", fragments=2)
        _write_pack_manifest(
            tmp_path,
            "x",
            [
                {"skill_id": "good", "file": "good.yaml", "fragment_count": 2},
            ],
        )
        manifest, errors = ip._read_pack_manifest(tmp_path)  # pyright: ignore[reportPrivateUsage]
        assert manifest is not None
        assert errors == []


# ---------------------------------------------------------------------------
# install_local_pack — top-level outcomes
# ---------------------------------------------------------------------------


class TestInstallLocalPack:
    def test_manifest_invalid_returns_early(self, tmp_path: Path) -> None:
        _write_pack_manifest(
            tmp_path,
            "bad",
            [
                {"skill_id": "ghost", "file": "ghost.yaml", "fragment_count": 1},
            ],
        )
        result = ip.install_local_pack(tmp_path, root=tmp_path)
        assert result["action"] == "manifest_invalid"
        assert any("file not found on disk" in e for e in result["errors"])

    def test_embedding_dim_mismatch_blocks(self, tmp_path: Path) -> None:
        _write_skill_yaml(tmp_path, "good", fragments=2)
        _write_pack_manifest(
            tmp_path,
            "x",
            [{"skill_id": "good", "file": "good.yaml", "fragment_count": 2}],
            embedding_dim=768,
        )
        # Fake a corpus that reports 1024-dim
        with patch.object(
            ip,
            "_check_embedding_dim",
            return_value="embedding dimension mismatch: pack expects 768-dim but corpus is 1024-dim.",
        ):
            result = ip.install_local_pack(tmp_path, root=tmp_path)
        assert result["action"] == "embedding_dim_mismatch"
        assert "768-dim" in result["error"]

    def test_outcome_classification(self, tmp_path: Path) -> None:
        """Partial failure triggers rollback — no skills remain ingested."""
        # Three fake ingest results, one of each
        fake_results = [
            {
                "yaml": "a.yaml",
                "exit_code": 0,
                "outcome": "ingested",
                "stdout_tail": "ok: loaded a",
                "stderr_tail": "",
            },
            {
                "yaml": "b.yaml",
                "exit_code": 4,
                "outcome": "duplicate",
                "stdout_tail": "",
                "stderr_tail": "skip: skill_id 'b' already exists",
            },
            {
                "yaml": "c.yaml",
                "exit_code": 2,
                "outcome": "failed",
                "stdout_tail": "",
                "stderr_tail": "validation error",
            },
        ]
        for sid in ("a", "b", "c"):
            _write_skill_yaml(tmp_path, sid)
        _write_pack_manifest(
            tmp_path,
            "x",
            [
                {"skill_id": "a", "file": "a.yaml", "fragment_count": 3},
                {"skill_id": "b", "file": "b.yaml", "fragment_count": 3},
                {"skill_id": "c", "file": "c.yaml", "fragment_count": 3},
            ],
        )
        # Stub embedding-dim check and the skill store for rollback
        mock_store = MagicMock()
        with (
            patch.object(ip, "_check_embedding_dim", return_value=None),
            patch.object(ip, "_ingest_yaml", side_effect=fake_results),
            patch.object(ip.install_state, "load_state", return_value={}),
            patch.object(ip.install_state, "save_state"),
            patch.object(ip.install_state, "record_step"),
            patch("agentalloy.config.get_settings") as mock_settings,
            patch.object(ip, "open_skills", return_value=mock_store),
        ):
            mock_settings.return_value.duckdb_path = str(tmp_path / "test.duck")
            # Default strict=True: the fixture is lint-clean, so Gate 1 passes
            # for real and _ingest_yaml's mocked outcomes drive rollback below.
            result = ip.install_local_pack(tmp_path, root=tmp_path)
        # Rollback: successfully ingested skills are deleted, so 0 remain
        assert result["skills_ingested"] == 0
        assert result["skills_already_present"] == 1
        assert result["ingest_failures"] == 1
        assert result["action"] == "ingested_with_errors"
        assert "rolled back" in (result.get("remediation") or "").lower()

    def test_embed_model_mismatch_soft_warns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Same dim, different model → warn but don't block."""
        from unittest.mock import MagicMock

        from agentalloy.install.subcommands import install_pack as _ip

        manifest = {"embedding_dim": 1024, "embed_model": "pack-model"}
        fake_vs = MagicMock()
        fake_vs.embedding_dim.return_value = 1024
        fake_settings = MagicMock()
        fake_settings.fragments_lance_path = "/tmp/fake.lance"
        fake_settings.runtime_embedding_model = "corpus-model"

        with (
            patch("agentalloy.config.get_settings", return_value=fake_settings),
            patch.object(_ip, "open_fragments", return_value=fake_vs),
        ):
            capsys.readouterr()  # clear any pre-existing capture
            result = _ip._check_embedding_dim(manifest, tmp_path)  # pyright: ignore[reportPrivateUsage]
            err_after = capsys.readouterr().err

        assert result is None  # not blocked
        assert "WARN" in err_after
        assert "pack-model" in err_after
        assert "corpus-model" in err_after

    def test_action_already_installed_when_all_duplicates(self, tmp_path: Path) -> None:
        """If every skill in the pack is already present, action is 'already_installed'."""
        _write_skill_yaml(tmp_path, "a")
        _write_pack_manifest(
            tmp_path, "x", [{"skill_id": "a", "file": "a.yaml", "fragment_count": 3}]
        )
        # Create corpus dir + files so the Pattern E corpus verification passes.
        # Verification checks the ingest path (settings.duckdb_path), so point it
        # at the seeded agentalloy.duck.
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        (corpus_dir / "agentalloy.duck").touch()
        (corpus_dir / "fragments.lance").mkdir()
        with (
            patch.object(ip, "_check_embedding_dim", return_value=None),
            patch.object(
                ip,
                "_ingest_yaml",
                return_value={
                    "yaml": "a.yaml",
                    "exit_code": 4,
                    "outcome": "duplicate",
                    "stdout_tail": "",
                    "stderr_tail": "skip",
                },
            ),
            patch.object(ip.install_state, "load_state", return_value={}),
            patch.object(ip.install_state, "save_state"),
            patch.object(ip.install_state, "record_step"),
            patch.object(ip.install_state, "corpus_dir", return_value=corpus_dir),
            patch(
                "agentalloy.config.get_settings",
                return_value=MagicMock(
                    duckdb_path=str(corpus_dir / "agentalloy.duck"),
                    fragments_lance_path=str(corpus_dir / "fragments.lance"),
                ),
            ),
        ):
            # Default strict=True: the fixture is lint-clean, so Gate 1 passes
            # for real and the mocked "duplicate" ingest outcome drives this.
            result = ip.install_local_pack(tmp_path, root=tmp_path)
        assert result["action"] == "already_installed"
        assert result["ingest_failures"] == 0


# ---------------------------------------------------------------------------
# Deprecation back-propagation — _propagate_deprecation / _ingest_yaml
# ---------------------------------------------------------------------------


def _seed_skill_node(db_path: str, skill_id: str) -> None:
    """Migrate a fresh skill store and insert a single live skill row."""
    from agentalloy.storage.skill_store import open_skill_store

    with open_skill_store(db_path) as store:
        store.migrate()
        store.execute(
            "INSERT INTO skills (skill_id, canonical_name, deprecated, always_apply) "
            "VALUES (?, ?, false, false)",
            [skill_id, skill_id],
        )


def _read_skill_flags(db_path: str, skill_id: str) -> tuple[Any, Any] | None:
    from agentalloy.storage.skill_store import open_skill_store

    with open_skill_store(db_path, read_only=True) as store:
        rows = store.execute(
            "SELECT deprecated, superseded_by FROM skills WHERE skill_id = ?",
            [skill_id],
        )
    if not rows:
        return None
    return rows[0][0], rows[0][1]


class TestDeprecationPropagation:
    def test_updates_existing_skill_node(self, tmp_path: Path) -> None:
        """A deprecated YAML whose skill_id is already ingested updates the row."""
        db_path = str(tmp_path / "agentalloy.duck")
        _seed_skill_node(db_path, "old-skill")

        fake_settings = MagicMock()
        fake_settings.duckdb_path = db_path
        with patch("agentalloy.config.get_settings", return_value=fake_settings):
            outcome = ip._propagate_deprecation("old-skill", "new-skill")  # pyright: ignore[reportPrivateUsage]

        assert outcome == "deprecated_updated"
        flags = _read_skill_flags(db_path, "old-skill")
        assert flags == (True, "new-skill")

    def test_skips_when_skill_absent(self, tmp_path: Path) -> None:
        """A deprecated YAML for a skill not in the graph is a plain skip."""
        db_path = str(tmp_path / "agentalloy.duck")
        _seed_skill_node(db_path, "present-skill")

        fake_settings = MagicMock()
        fake_settings.duckdb_path = db_path
        with patch("agentalloy.config.get_settings", return_value=fake_settings):
            outcome = ip._propagate_deprecation("missing-skill", "x")  # pyright: ignore[reportPrivateUsage]

        assert outcome == "deprecated"
        # The unrelated row is untouched.
        assert _read_skill_flags(db_path, "present-skill") == (False, None)

    def test_lock_held_does_not_crash(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A lock-held error warns with a FIX hint and degrades to a skip."""
        fake_settings = MagicMock()
        fake_settings.duckdb_path = str(tmp_path / "agentalloy.duck")
        boom = MagicMock(side_effect=RuntimeError("Could not set lock on file: held by PID 999"))
        with (
            patch("agentalloy.config.get_settings", return_value=fake_settings),
            patch.object(ip, "open_skills", boom),
        ):
            capsys.readouterr()
            outcome = ip._propagate_deprecation("old-skill", "new-skill")  # pyright: ignore[reportPrivateUsage]
            err = capsys.readouterr().err

        assert outcome == "deprecated"
        assert "WARN" in err
        assert "FIX" in err

    def test_ingest_yaml_deprecated_branch_propagates(self, tmp_path: Path) -> None:
        """_ingest_yaml on a deprecated YAML reports deprecated_updated when the row exists."""
        db_path = str(tmp_path / "agentalloy.duck")
        _seed_skill_node(db_path, "dep-skill")

        yaml_path = tmp_path / "dep.yaml"
        yaml_path.write_text(
            yaml.safe_dump(
                {
                    "skill_id": "dep-skill",
                    "deprecated": True,
                    "superseded_by": "fresh-skill",
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        fake_settings = MagicMock()
        fake_settings.duckdb_path = db_path
        with patch("agentalloy.config.get_settings", return_value=fake_settings):
            result = ip._ingest_yaml(yaml_path, tmp_path)  # pyright: ignore[reportPrivateUsage]

        assert result["outcome"] == "deprecated_updated"
        assert _read_skill_flags(db_path, "dep-skill") == (True, "fresh-skill")


# ---------------------------------------------------------------------------
# install_pack(name_or_path) — auto-routes path → local-pack
# ---------------------------------------------------------------------------


class TestInstallPackAutoRoute:
    def test_directory_with_pack_yaml_routes_to_local(self, tmp_path: Path, monkeypatch) -> None:
        _write_skill_yaml(tmp_path, "good", fragments=2)
        _write_pack_manifest(
            tmp_path, "x", [{"skill_id": "good", "file": "good.yaml", "fragment_count": 2}]
        )
        # Force write_host so the local-dir branch installs directly rather than
        # routing to a running service (keeps the test hermetic).
        from agentalloy.install import corpus_write_route as cwr

        monkeypatch.setattr(
            cwr, "decide_corpus_write_route", lambda: cwr.CorpusWriteRoute("write_host")
        )
        with patch.object(
            ip, "install_local_pack", return_value={"action": "ingested", "pack": "x"}
        ) as m:
            result = ip.install_pack(str(tmp_path), root=tmp_path)
        assert result["action"] == "ingested"
        m.assert_called_once()


# ---------------------------------------------------------------------------
# install_packs picker — _ordered_with_deps warns on missing deps
# ---------------------------------------------------------------------------


class TestPickerDepWarning:
    def test_missing_dep_warns(self, capsys: pytest.CaptureFixture[str]) -> None:
        available = {
            "a": {"depends_on": ["nonexistent-dep"]},
            "core": {"depends_on": []},
        }
        ips._ordered_with_deps({"a", "core"}, available)  # pyright: ignore[reportPrivateUsage]
        err = capsys.readouterr().err
        assert "nonexistent-dep" in err
        assert "depends_on" in err

    def test_present_dep_no_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        available = {
            "core": {"depends_on": []},
            "react": {"depends_on": ["typescript"]},
            "typescript": {"depends_on": []},
        }
        ips._ordered_with_deps({"react"}, available)  # pyright: ignore[reportPrivateUsage]
        err = capsys.readouterr().err
        assert "depends_on" not in err

    def test_topo_order_dep_before_dependent(self) -> None:
        available = {
            "core": {"depends_on": []},
            "typescript": {"depends_on": []},
            "react": {"depends_on": ["typescript"]},
            "nextjs": {"depends_on": ["react"]},
        }
        ordered = ips._ordered_with_deps({"nextjs"}, available)  # pyright: ignore[reportPrivateUsage]
        # typescript before react before nextjs
        assert ordered.index("typescript") < ordered.index("react") < ordered.index("nextjs")


# ---------------------------------------------------------------------------
# install_packs._select_packs — flag handling
# ---------------------------------------------------------------------------


class TestPackSelector:
    def test_explicit_packs_flag_includes_always_on(self) -> None:
        available = {
            "core": {"always_install": True, "depends_on": []},
            "engineering": {"always_install": True, "depends_on": []},
            "nodejs": {"always_install": False, "depends_on": []},
            "vue": {"always_install": False, "depends_on": []},
        }
        chosen, unknown, _ = ips._select_packs(available, "nodejs", interactive=False)  # pyright: ignore[reportPrivateUsage]
        assert "core" in chosen
        assert "engineering" in chosen
        assert "nodejs" in chosen
        assert "vue" not in chosen
        assert unknown == []

    def test_all_keyword(self) -> None:
        available = {
            "core": {"always_install": True, "depends_on": []},
            "vue": {"always_install": False, "depends_on": []},
            "nodejs": {"always_install": False, "depends_on": []},
        }
        chosen, unknown, _ = ips._select_packs(available, "all", interactive=False)  # pyright: ignore[reportPrivateUsage]
        assert set(chosen) >= {"core", "vue", "nodejs"}
        assert unknown == []

    def test_non_interactive_no_flag_only_always_on(self) -> None:
        available = {
            "core": {"always_install": True, "depends_on": []},
            "engineering": {"always_install": True, "depends_on": []},
            "nodejs": {"always_install": False, "depends_on": []},
        }
        chosen, unknown, _ = ips._select_packs(available, None, interactive=False)  # pyright: ignore[reportPrivateUsage]
        assert set(chosen) == {"core", "engineering"}
        assert unknown == []

    def test_unknown_pack_in_flag_surfaced(self) -> None:
        """`_select_packs` returns unknown names; the caller decides
        whether to fail-fast or fall through with --ignore-unknown."""
        available = {
            "core": {"always_install": True, "depends_on": []},
            "nodejs": {"always_install": False, "depends_on": []},
        }
        chosen, unknown, _ = ips._select_packs(available, "nodejs,nonexistent", interactive=False)  # pyright: ignore[reportPrivateUsage]
        assert "nodejs" in chosen
        assert "nonexistent" not in chosen
        assert unknown == ["nonexistent"]


# ---------------------------------------------------------------------------
# Corpus-aware version-gate skip — _corpus_missing_active / install_local_pack
# ---------------------------------------------------------------------------


def _seed_active_skill(db_path: str, skill_id: str) -> None:
    """Fresh store with one skill whose current version is active."""
    from agentalloy.storage.skill_store import open_skill_store

    with open_skill_store(db_path) as store:
        store.migrate()
        store.execute(
            "INSERT INTO skills (skill_id, canonical_name, deprecated, always_apply, "
            "current_version_id) VALUES (?, ?, false, false, ?)",
            [skill_id, skill_id, f"{skill_id}-v1"],
        )
        store.execute(
            "INSERT INTO skill_versions (version_id, skill_id, version_number, status, "
            "raw_prose) VALUES (?, ?, 1, 'active', 'p')",
            [f"{skill_id}-v1", skill_id],
        )


class TestCorpusMissingActive:
    def _settings(self, db_path: Path) -> MagicMock:
        return MagicMock(duckdb_path=str(db_path))

    def test_present_active_skill_is_not_missing(self, tmp_path: Path) -> None:
        db = tmp_path / "agentalloy.duck"
        _seed_active_skill(str(db), "a")
        with patch("agentalloy.config.get_settings", return_value=self._settings(db)):
            assert ip._corpus_missing_active(["a"]) == []  # pyright: ignore[reportPrivateUsage]

    def test_absent_skill_is_missing(self, tmp_path: Path) -> None:
        db = tmp_path / "agentalloy.duck"
        _seed_active_skill(str(db), "a")
        with patch("agentalloy.config.get_settings", return_value=self._settings(db)):
            missing = ip._corpus_missing_active(["a", "b"])  # pyright: ignore[reportPrivateUsage]
        assert missing == ["b"]

    def test_no_store_file_reports_all_missing(self, tmp_path: Path) -> None:
        db = tmp_path / "never-created.duck"
        with patch("agentalloy.config.get_settings", return_value=self._settings(db)):
            assert ip._corpus_missing_active(["a", "b"]) == ["a", "b"]  # pyright: ignore[reportPrivateUsage]

    def test_unreadable_store_reports_all_missing(self, tmp_path: Path) -> None:
        # A wrong skip is unfixable; a redundant re-ingest is idempotent.
        db = tmp_path / "agentalloy.duck"
        db.write_text("not a duckdb file")
        with patch("agentalloy.config.get_settings", return_value=self._settings(db)):
            assert ip._corpus_missing_active(["a"]) == ["a"]  # pyright: ignore[reportPrivateUsage]

    def test_empty_id_list_short_circuits(self) -> None:
        assert ip._corpus_missing_active([]) == []  # pyright: ignore[reportPrivateUsage]


class TestCorpusAwareSkip:
    """The version gate's 'already_installed' must be confirmed against the
    corpus: a registry that outlives the store (engine migration, wiped
    corpus) otherwise wedges install-packs into a skip no re-run can fix —
    the exact sdd-only-corpus failure after the v4→v5 migration."""

    def _install(self, tmp_path: Path, *, seed_corpus_skill: bool):
        from agentalloy.pack_validation import content_hash

        _write_skill_yaml(tmp_path, "a")
        entries = [{"skill_id": "a", "file": "a.yaml", "fragment_count": 3}]
        _write_pack_manifest(tmp_path, "x", entries)

        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        db_path = corpus_dir / "agentalloy.duck"
        (corpus_dir / "fragments.lance").mkdir()
        if seed_corpus_skill:
            _seed_active_skill(str(db_path), "a")
        else:
            from agentalloy.storage.skill_store import open_skill_store

            with open_skill_store(str(db_path)) as store:  # real, migrated, empty
                store.migrate()

        # Registry says pack x is installed with EXACTLY this content.
        state = {
            "installed_packs": [
                {
                    "name": "x",
                    "version": "1.0.0",
                    "content_hash": content_hash(tmp_path, entries),
                }
            ]
        }
        ingest_forces: list[bool] = []

        def fake_ingest(yaml_path, root, *, no_restart, force, strict):
            ingest_forces.append(force)
            return {
                "yaml": "a.yaml",
                "exit_code": 0,
                "outcome": "ingested",
                "stdout_tail": "",
                "stderr_tail": "",
            }

        with (
            patch.object(ip, "_check_embedding_dim", return_value=None),
            patch.object(ip, "_ingest_yaml", side_effect=fake_ingest),
            patch.object(ip.install_state, "load_state", return_value=state),
            patch.object(ip.install_state, "save_state"),
            patch.object(ip.install_state, "record_step"),
            patch.object(ip.install_state, "corpus_dir", return_value=corpus_dir),
            patch(
                "agentalloy.config.get_settings",
                return_value=MagicMock(
                    duckdb_path=str(db_path),
                    fragments_lance_path=str(corpus_dir / "fragments.lance"),
                ),
            ),
        ):
            result = ip.install_local_pack(tmp_path, root=tmp_path, run_reembed=False)
        return result, ingest_forces

    def test_skip_honored_when_corpus_has_the_skills(self, tmp_path: Path) -> None:
        result, forces = self._install(tmp_path, seed_corpus_skill=True)
        assert result["action"] == "already_installed"
        assert forces == []  # no ingest ran

    def test_skip_overridden_when_corpus_is_missing_the_skills(self, tmp_path: Path) -> None:
        result, forces = self._install(tmp_path, seed_corpus_skill=False)
        assert result["action"] == "ingested"
        assert forces == [True]  # fell through to a FORCE re-ingest


class TestExpectedActiveSkillIds:
    def test_excludes_deprecated_tombstones(self, tmp_path: Path) -> None:
        from agentalloy.pack_validation import expected_active_skill_ids

        (tmp_path / "live.yaml").write_text("skill_id: live\n", encoding="utf-8")
        (tmp_path / "dead.yaml").write_text("skill_id: dead\ndeprecated: true\n", encoding="utf-8")
        entries = [
            {"skill_id": "live", "file": "live.yaml"},
            {"skill_id": "dead", "file": "dead.yaml"},
        ]
        assert expected_active_skill_ids(tmp_path, entries) == ["live"]

    def test_skill_id_falls_back_to_yaml(self, tmp_path: Path) -> None:
        from agentalloy.pack_validation import expected_active_skill_ids

        (tmp_path / "a.yaml").write_text("skill_id: from-yaml\n", encoding="utf-8")
        assert expected_active_skill_ids(tmp_path, [{"file": "a.yaml"}]) == ["from-yaml"]

    def test_unreadable_yaml_stays_included(self, tmp_path: Path) -> None:
        # Counting one too many beats silently expecting one too few.
        from agentalloy.pack_validation import expected_active_skill_ids

        (tmp_path / "bad.yaml").write_text(":\n\t- not yaml", encoding="utf-8")
        entries = [{"skill_id": "kept", "file": "bad.yaml"}]
        assert expected_active_skill_ids(tmp_path, entries) == ["kept"]
