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


def _write_skill_yaml(
    pack_dir: Path,
    skill_id: str,
    *,
    fragments: int = 3,
    canonical_name: str | None = None,
) -> Path:
    """Write a minimal valid domain-skill YAML."""
    fy = [
        {
            "sequence": i + 1,
            "fragment_type": "execution" if i == 0 else "rationale",
            "content": (
                f"This is fragment {i + 1} content with sufficient words to pass validation."
            ),
        }
        for i in range(fragments)
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
        "raw_prose": f"# {skill_id}\n\ntest body",
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
            _write_skill_yaml(tmp_path, sid, fragments=2)
        _write_pack_manifest(
            tmp_path,
            "x",
            [
                {"skill_id": "a", "file": "a.yaml", "fragment_count": 2},
                {"skill_id": "b", "file": "b.yaml", "fragment_count": 2},
                {"skill_id": "c", "file": "c.yaml", "fragment_count": 2},
            ],
        )
        # Stub embedding-dim check and LadybugStore for rollback
        mock_store = MagicMock()
        mock_store.open = MagicMock()
        mock_store.close = MagicMock()
        with (
            patch.object(ip, "_check_embedding_dim", return_value=None),
            patch.object(ip, "_ingest_yaml", side_effect=fake_results),
            patch.object(ip.install_state, "load_state", return_value={}),
            patch.object(ip.install_state, "save_state"),
            patch.object(ip.install_state, "record_step"),
            patch("agentalloy.config.get_settings") as mock_settings,
            patch(
                "agentalloy.install.subcommands.install_pack.LadybugStore", return_value=mock_store
            ),
        ):
            mock_settings.return_value.ladybug_db_path = str(tmp_path / "test.duck")
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
        fake_settings.duckdb_path = "/tmp/fake.duck"
        fake_settings.runtime_embedding_model = "corpus-model"

        with (
            patch("agentalloy.config.get_settings", return_value=fake_settings),
            patch(
                "agentalloy.storage.vector_store.open_or_create",
                return_value=MagicMock(__enter__=lambda s: fake_vs, __exit__=lambda *a: None),
            ),
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
        _write_skill_yaml(tmp_path, "a", fragments=2)
        _write_pack_manifest(
            tmp_path, "x", [{"skill_id": "a", "file": "a.yaml", "fragment_count": 2}]
        )
        # Create corpus dir + files so the Pattern E corpus verification passes.
        # Verification checks the ingest paths (settings.duckdb_path /
        # ladybug_db_path), so point those at the seeded files.
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        (corpus_dir / "skills.duck").touch()
        (corpus_dir / "ladybug").mkdir()
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
                    duckdb_path=str(corpus_dir / "skills.duck"),
                    ladybug_db_path=str(corpus_dir / "ladybug"),
                ),
            ),
        ):
            result = ip.install_local_pack(tmp_path, root=tmp_path)
        assert result["action"] == "already_installed"
        assert result["ingest_failures"] == 0


# ---------------------------------------------------------------------------
# Deprecation back-propagation — _propagate_deprecation / _ingest_yaml
# ---------------------------------------------------------------------------


def _seed_skill_node(db_path: str, skill_id: str) -> None:
    """Migrate a fresh LadybugDB and insert a single live Skill node."""
    from agentalloy.storage.ladybug import LadybugStore

    with LadybugStore(db_path) as store:
        store.migrate()
        store.execute(
            "CREATE (s:Skill {skill_id: $sid, canonical_name: $sid, "
            "deprecated: false, always_apply: false})",
            {"sid": skill_id},
        )


def _read_skill_flags(db_path: str, skill_id: str) -> tuple[Any, Any] | None:
    from agentalloy.storage.ladybug import LadybugStore

    with LadybugStore(db_path) as store:
        rows = store.execute(
            "MATCH (s:Skill {skill_id: $sid}) RETURN s.deprecated, s.superseded_by",
            {"sid": skill_id},
        )
    if not rows:
        return None
    return rows[0][0], rows[0][1]


class TestDeprecationPropagation:
    def test_updates_existing_skill_node(self, tmp_path: Path) -> None:
        """A deprecated YAML whose skill_id is already ingested updates the node."""
        db_path = str(tmp_path / "ladybug")
        _seed_skill_node(db_path, "old-skill")

        fake_settings = MagicMock()
        fake_settings.ladybug_db_path = db_path
        with patch("agentalloy.config.get_settings", return_value=fake_settings):
            outcome = ip._propagate_deprecation("old-skill", "new-skill")  # pyright: ignore[reportPrivateUsage]

        assert outcome == "deprecated_updated"
        flags = _read_skill_flags(db_path, "old-skill")
        assert flags == (True, "new-skill")

    def test_skips_when_skill_absent(self, tmp_path: Path) -> None:
        """A deprecated YAML for a skill not in the graph is a plain skip."""
        db_path = str(tmp_path / "ladybug")
        _seed_skill_node(db_path, "present-skill")

        fake_settings = MagicMock()
        fake_settings.ladybug_db_path = db_path
        with patch("agentalloy.config.get_settings", return_value=fake_settings):
            outcome = ip._propagate_deprecation("missing-skill", "x")  # pyright: ignore[reportPrivateUsage]

        assert outcome == "deprecated"
        # The unrelated node is untouched.
        assert _read_skill_flags(db_path, "present-skill") == (False, None)

    def test_lock_held_does_not_crash(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A lock-held error warns with a FIX hint and degrades to a skip."""
        fake_settings = MagicMock()
        fake_settings.ladybug_db_path = str(tmp_path / "ladybug")
        boom = MagicMock(side_effect=RuntimeError("Could not set lock on file: held by PID 999"))
        with (
            patch("agentalloy.config.get_settings", return_value=fake_settings),
            patch.object(ip, "LadybugStore", boom),
        ):
            capsys.readouterr()
            outcome = ip._propagate_deprecation("old-skill", "new-skill")  # pyright: ignore[reportPrivateUsage]
            err = capsys.readouterr().err

        assert outcome == "deprecated"
        assert "WARN" in err
        assert "FIX" in err

    def test_ingest_yaml_deprecated_branch_propagates(self, tmp_path: Path) -> None:
        """_ingest_yaml on a deprecated YAML reports deprecated_updated when the node exists."""
        db_path = str(tmp_path / "ladybug")
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
        fake_settings.ladybug_db_path = db_path
        with patch("agentalloy.config.get_settings", return_value=fake_settings):
            result = ip._ingest_yaml(yaml_path, tmp_path)  # pyright: ignore[reportPrivateUsage]

        assert result["outcome"] == "deprecated_updated"
        assert _read_skill_flags(db_path, "dep-skill") == (True, "fresh-skill")


# ---------------------------------------------------------------------------
# install_pack(name_or_path) — auto-routes path → local-pack
# ---------------------------------------------------------------------------


class TestInstallPackAutoRoute:
    def test_directory_with_pack_yaml_routes_to_local(self, tmp_path: Path) -> None:
        _write_skill_yaml(tmp_path, "good", fragments=2)
        _write_pack_manifest(
            tmp_path, "x", [{"skill_id": "good", "file": "good.yaml", "fragment_count": 2}]
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
