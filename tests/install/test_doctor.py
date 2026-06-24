"""Unit tests for the ``doctor`` subcommand (new comprehensive implementation)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from agentalloy.install.subcommands.doctor import (
    SCHEMA_VERSION,
    _check_config,  # pyright: ignore[reportPrivateUsage]
    _check_corpus_count,  # pyright: ignore[reportPrivateUsage]
    _check_corpus_files,  # pyright: ignore[reportPrivateUsage]
    _check_embed_server,  # pyright: ignore[reportPrivateUsage]
    _check_embedding_dim,  # pyright: ignore[reportPrivateUsage]
    _check_ladybug_schema,  # pyright: ignore[reportPrivateUsage]
    _check_pack_manifests,  # pyright: ignore[reportPrivateUsage]
    _check_service,  # pyright: ignore[reportPrivateUsage]
    _repair,  # pyright: ignore[reportPrivateUsage]
    _repair_container,  # pyright: ignore[reportPrivateUsage]
    _run_doctor_container,  # pyright: ignore[reportPrivateUsage]
    run_doctor,
)

# ---------------------------------------------------------------------------
# Check 1: config
# ---------------------------------------------------------------------------


class TestCheckConfig:
    def test_missing_env_fails(self, tmp_path: Path) -> None:
        result = _check_config()
        # XDG isolation means .env won't exist in tmp dir
        assert result["passed"] is False
        assert result["name"] == "config"
        assert "remediation" in result

    def test_present_with_required_keys_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "xdg-config" / "agentalloy"
        config_dir.mkdir(parents=True)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
        env_file = config_dir / ".env"
        env_file.write_text(
            "RUNTIME_EMBED_BASE_URL=http://localhost:11434\n"
            "RUNTIME_EMBEDDING_MODEL=nomic-embed-text-v1.5\n"
        )
        result = _check_config()
        assert result["passed"] is True
        assert "RUNTIME_EMBED_BASE_URL" in result.get("detail", "")

    def test_missing_key_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / "xdg-config" / "agentalloy"
        config_dir.mkdir(parents=True)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
        env_file = config_dir / ".env"
        env_file.write_text("RUNTIME_EMBED_BASE_URL=http://localhost:11434\n")
        result = _check_config()
        assert result["passed"] is False
        assert "RUNTIME_EMBEDDING_MODEL" in result["error"]


# ---------------------------------------------------------------------------
# Check 2: embed_server
# ---------------------------------------------------------------------------


class TestCheckEmbedServer:
    def test_server_unreachable_fails(self) -> None:
        with patch(
            "agentalloy.install.subcommands.doctor.urlopen", side_effect=URLError("refused")
        ):
            result = _check_embed_server("http://localhost:47951", "nomic-embed-text-v1.5")
        assert result["passed"] is False
        assert result["name"] == "embed_server"
        assert "remediation" in result

    def test_server_reachable_passes(self) -> None:
        mock_resp = MagicMock()
        mock_resp.read.return_value = b""
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("agentalloy.install.subcommands.doctor.urlopen", return_value=mock_resp):
            result = _check_embed_server("http://localhost:47951", "nomic-embed-text-v1.5")
        assert result["passed"] is True

    def test_server_http_status_to_get_passes(self) -> None:
        # llama-server returns HTTP 415 to a GET (it wants POST) — that's an HTTP
        # status response, so the server is UP. Must not be a false negative.
        http_415 = HTTPError("http://localhost:47951", 415, "Unsupported Media Type", {}, None)
        with patch("agentalloy.install.subcommands.doctor.urlopen", side_effect=http_415):
            result = _check_embed_server("http://localhost:47951", "nomic-embed-text-v1.5")
        assert result["passed"] is True
        assert result.get("severity") != "warn"


# ---------------------------------------------------------------------------
# Check 3: corpus_files
# ---------------------------------------------------------------------------


class TestCheckCorpusFiles:
    def test_missing_files_fails(self, tmp_path: Path) -> None:
        result = _check_corpus_files(tmp_path)
        assert result["passed"] is False
        assert "ladybug" in result["error"] or "skills.duck" in result["error"]

    def test_both_present_passes(self, tmp_path: Path) -> None:
        (tmp_path / "ladybug").mkdir()
        (tmp_path / "skills.duck").write_bytes(b"")
        result = _check_corpus_files(tmp_path)
        assert result["passed"] is True


# ---------------------------------------------------------------------------
# Check 4: ladybug_schema
# ---------------------------------------------------------------------------


class TestCheckLadybugSchema:
    def test_schema_missing_fails(self, tmp_path: Path) -> None:
        # DB file present but the Skill table is missing — the open succeeds, the
        # query raises. (File must exist or the absent-corpus guard fires first.)
        (tmp_path / "ladybug").write_bytes(b"")
        with patch("agentalloy.storage.ladybug.LadybugStore") as mock_cls:
            mock_store = MagicMock()
            mock_store.__enter__ = MagicMock(return_value=mock_store)
            mock_store.__exit__ = MagicMock(return_value=False)
            mock_store.execute.side_effect = Exception("Table Skill does not exist")
            mock_cls.return_value = mock_store
            result = _check_ladybug_schema(str(tmp_path / "ladybug"))
        assert result["passed"] is False
        assert result.get("lock_held") is False
        assert "remediation" in result

    def test_lock_held_sets_flag(self, tmp_path: Path) -> None:
        (tmp_path / "ladybug").write_bytes(b"")
        with patch("agentalloy.storage.ladybug.LadybugStore") as mock_cls:
            mock_store = MagicMock()
            mock_store.__enter__ = MagicMock(return_value=mock_store)
            mock_store.__exit__ = MagicMock(return_value=False)
            mock_store.execute.side_effect = Exception("Could not set lock on file /corpus/ladybug")
            mock_cls.return_value = mock_store
            result = _check_ladybug_schema(str(tmp_path / "ladybug"))
        assert result["passed"] is False
        assert result["lock_held"] is True
        assert "remediation" in result

    def test_schema_ok_passes(self, tmp_path: Path) -> None:
        (tmp_path / "ladybug").write_bytes(b"")
        with patch("agentalloy.storage.ladybug.LadybugStore") as mock_cls:
            mock_store = MagicMock()
            mock_store.__enter__ = MagicMock(return_value=mock_store)
            mock_store.__exit__ = MagicMock(return_value=False)
            mock_store.execute.return_value = [[1]]
            mock_cls.return_value = mock_store
            result = _check_ladybug_schema(str(tmp_path / "ladybug"))
        assert result["passed"] is True


# ---------------------------------------------------------------------------
# Check 5: corpus_count
# ---------------------------------------------------------------------------


class TestCheckCorpusCount:
    def test_empty_corpus_fails(self, tmp_path: Path) -> None:
        (tmp_path / "ladybug").write_bytes(b"")
        (tmp_path / "skills.duck").write_bytes(b"")
        with (
            patch("agentalloy.storage.ladybug.LadybugStore") as mock_cls,
            patch("agentalloy.storage.vector_store.open_or_create") as mock_oc,
        ):
            mock_store = MagicMock()
            mock_store.__enter__ = MagicMock(return_value=mock_store)
            mock_store.__exit__ = MagicMock(return_value=False)
            mock_store.execute.return_value = [[0]]
            mock_cls.return_value = mock_store
            mock_vs = MagicMock()
            mock_vs.count_embeddings.return_value = 0
            mock_oc.return_value = mock_vs
            result = _check_corpus_count(str(tmp_path / "ladybug"), str(tmp_path / "skills.duck"))
        assert result["passed"] is False

    def test_populated_corpus_passes(self, tmp_path: Path) -> None:
        (tmp_path / "ladybug").write_bytes(b"")
        (tmp_path / "skills.duck").write_bytes(b"")
        with (
            patch("agentalloy.storage.ladybug.LadybugStore") as mock_cls,
            patch("agentalloy.storage.vector_store.open_or_create") as mock_oc,
        ):
            mock_store = MagicMock()
            mock_store.__enter__ = MagicMock(return_value=mock_store)
            mock_store.__exit__ = MagicMock(return_value=False)
            mock_store.execute.return_value = [[30]]
            mock_cls.return_value = mock_store
            mock_vs = MagicMock()
            mock_vs.count_embeddings.return_value = 100
            mock_oc.return_value = mock_vs
            result = _check_corpus_count(str(tmp_path / "ladybug"), str(tmp_path / "skills.duck"))
        assert result["passed"] is True
        assert "30 skills" in result["detail"]


# ---------------------------------------------------------------------------
# Check 6: embedding_dim
# ---------------------------------------------------------------------------


class TestCheckEmbeddingDim:
    def test_dim_mismatch_fails(self, tmp_path: Path) -> None:
        with patch("agentalloy.storage.vector_store.open_or_create") as mock_oc:
            mock_vs = MagicMock()
            mock_vs.embedding_dim.return_value = 1024
            mock_oc.return_value = mock_vs
            result = _check_embedding_dim(str(tmp_path / "skills.duck"))
        assert result["passed"] is False
        assert "1024" in result["error"]

    def test_dim_match_passes(self, tmp_path: Path) -> None:
        with patch("agentalloy.storage.vector_store.open_or_create") as mock_oc:
            mock_vs = MagicMock()
            mock_vs.embedding_dim.return_value = 768
            mock_oc.return_value = mock_vs
            result = _check_embedding_dim(str(tmp_path / "skills.duck"))
        assert result["passed"] is True

    def test_empty_corpus_passes(self, tmp_path: Path) -> None:
        with patch("agentalloy.storage.vector_store.open_or_create") as mock_oc:
            mock_vs = MagicMock()
            mock_vs.embedding_dim.return_value = None
            mock_oc.return_value = mock_vs
            result = _check_embedding_dim(str(tmp_path / "skills.duck"))
        assert result["passed"] is True

    def test_dim_mismatch_via_guard_surfaces_tailored_message(self, tmp_path: Path) -> None:
        """When open_or_create's guard itself raises, show the tailored dim-mismatch."""
        from agentalloy.storage.vector_store import EmbeddingDimMismatch

        with (
            patch(
                "agentalloy.storage.vector_store.open_or_create",
                side_effect=EmbeddingDimMismatch("corpus 1024 vs 768"),
            ),
            patch(
                "agentalloy.install.subcommands.doctor._read_stored_dim",
                return_value=1024,
            ),
        ):
            result = _check_embedding_dim(str(tmp_path / "skills.duck"))
        assert result["passed"] is False
        assert "Stored dim 1024" in result["error"]
        assert "reembed --force" in result["remediation"]


# ---------------------------------------------------------------------------
# Check 7: service
# ---------------------------------------------------------------------------


class TestCheckService:
    def test_service_down_passes(self) -> None:
        with patch(
            "agentalloy.install.subcommands.doctor.urlopen", side_effect=URLError("refused")
        ):
            result = _check_service(47950)
        assert result["passed"] is True
        assert "not running" in result["detail"]

    def test_service_up_ok_passes(self) -> None:
        body = json.dumps({"status": "ok"}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("agentalloy.install.subcommands.doctor.urlopen", return_value=mock_resp):
            result = _check_service(47950)
        assert result["passed"] is True
        assert result.get("severity") != "warn"

    def test_service_up_healthy_passes(self) -> None:
        # /health emits "healthy" (not "ok") when all deps are ok — must be clean.
        body = json.dumps(
            {"status": "healthy", "dependencies": {"runtime_store": {"status": "ok"}}}
        ).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("agentalloy.install.subcommands.doctor.urlopen", return_value=mock_resp):
            result = _check_service(47950)
        assert result["passed"] is True
        assert result.get("severity") != "warn"
        assert "healthy" in result["detail"]

    def test_service_up_degraded_warns(self) -> None:
        body = json.dumps({"status": "degraded"}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("agentalloy.install.subcommands.doctor.urlopen", return_value=mock_resp):
            result = _check_service(47950)
        assert result["passed"] is True
        assert result.get("severity") == "warn"


# ---------------------------------------------------------------------------
# Check 8: pack_manifests
# ---------------------------------------------------------------------------


class TestCheckPackManifests:
    @pytest.fixture(autouse=True)
    def _warm_pack_validation_import(self) -> None:
        # _check_pack_manifests lazily does `from agentalloy.pack_validation import …`
        # (via install_pack). These tests patch sys.modules['agentalloy'] with a
        # MagicMock, which breaks that lazy import unless the submodule is already
        # cached — warm it here so the test is self-contained (not order-dependent).
        import agentalloy.pack_validation  # noqa: F401

    def test_valid_manifests_pass(self, tmp_path: Path) -> None:
        # _packs lives next to the agentalloy package __init__.py
        pkg_dir = tmp_path / "agentalloy"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        packs_root = pkg_dir / "_packs"
        packs_root.mkdir()
        for i in range(3):
            pack_dir = packs_root / f"pack{i}"
            pack_dir.mkdir()
            # Must satisfy the full _read_pack_manifest drift validation:
            # required fields, valid tier, and skill entries matching files.
            (pack_dir / f"skill{i}.yaml").write_text(
                f"skill_id: skill{i}\nfragments:\n  - seq: 1\n    content: x\n"
            )
            (pack_dir / "pack.yaml").write_text(
                f"name: pack{i}\n"
                "version: 1.0.0\n"
                "tier: foundation\n"
                "embed_model: nomic-embed-text-v1.5.Q8_0.gguf\n"
                "embedding_dim: 768\n"
                "skills:\n"
                f"- skill_id: skill{i}\n"
                f"  file: skill{i}.yaml\n"
                "  fragment_count: 1\n"
            )

        fake_module = MagicMock()
        fake_module.__file__ = str(pkg_dir / "__init__.py")
        with patch.dict("sys.modules", {"agentalloy": fake_module}):
            result = _check_pack_manifests()
        assert result["passed"] is True
        assert "3 pack manifest" in result["detail"]

    def test_corrupt_manifest_fails(self, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "agentalloy"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        packs_root = pkg_dir / "_packs"
        packs_root.mkdir()
        pack_dir = packs_root / "bad_pack"
        pack_dir.mkdir()
        # Missing 'name' key → bad manifest
        (pack_dir / "pack.yaml").write_text("skills: []\n")

        fake_module = MagicMock()
        fake_module.__file__ = str(pkg_dir / "__init__.py")
        with patch.dict("sys.modules", {"agentalloy": fake_module}):
            result = _check_pack_manifests()
        assert result["passed"] is False


# ---------------------------------------------------------------------------
# Full run_doctor
# ---------------------------------------------------------------------------


def _patch_all_checks(
    *,
    config_ok: bool = True,
    embed_ok: bool = True,
    corpus_files_ok: bool = True,
    schema_ok: bool = True,
    lock_held: bool = False,
    count_ok: bool = True,
    dim_ok: bool = True,
    service_ok: bool = True,
    manifests_ok: bool = True,
) -> Any:
    """Context manager that patches all individual checks for run_doctor isolation."""
    import contextlib

    @contextlib.contextmanager  # type: ignore[arg-type]
    def _ctx() -> Any:  # type: ignore[misc]
        with (
            patch(
                "agentalloy.install.subcommands.doctor._check_config",
                return_value={"name": "config", "passed": config_ok},
            ),
            patch(
                "agentalloy.install.subcommands.doctor._check_embed_server",
                return_value={"name": "embed_server", "passed": embed_ok},
            ),
            patch(
                "agentalloy.install.subcommands.doctor._check_corpus_files",
                return_value={"name": "corpus_files", "passed": corpus_files_ok},
            ),
            patch(
                "agentalloy.install.subcommands.doctor._check_ladybug_schema",
                return_value={
                    "name": "ladybug_schema",
                    "passed": schema_ok,
                    "lock_held": lock_held,
                },
            ),
            patch(
                "agentalloy.install.subcommands.doctor._check_corpus_count",
                return_value={"name": "corpus_count", "passed": count_ok},
            ),
            patch(
                "agentalloy.install.subcommands.doctor._check_embedding_dim",
                return_value={"name": "embedding_dim", "passed": dim_ok},
            ),
            patch(
                "agentalloy.install.subcommands.doctor._check_service",
                return_value={"name": "service", "passed": service_ok},
            ),
            patch(
                "agentalloy.install.subcommands.doctor._check_pack_manifests",
                return_value={"name": "pack_manifests", "passed": manifests_ok},
            ),
            patch(
                "agentalloy.install.subcommands.doctor._check_orphans",
                return_value={"name": "orphans", "passed": True},
            ),
        ):
            yield

    return _ctx()


class TestRunDoctorAllGreen:
    def test_all_passed_when_all_green(self) -> None:
        with _patch_all_checks():
            result = run_doctor()
        assert result["all_checks_passed"] is True
        assert result["schema_version"] == SCHEMA_VERSION
        names = [c["name"] for c in result["checks"]]
        assert names == [
            "config",
            "embed_server",
            "corpus_files",
            "ladybug_schema",
            "corpus_count",
            "embedding_dim",
            "service",
            "pack_manifests",
            "reranker",
            "orphans",
        ]

    def test_all_checks_have_name_and_passed(self) -> None:
        with _patch_all_checks():
            result = run_doctor()
        for check in result["checks"]:
            assert "name" in check
            assert "passed" in check

    def test_missing_env_sets_not_all_passed(self) -> None:
        # config fails → all_checks_passed False
        with _patch_all_checks(config_ok=False):
            result = run_doctor()
        assert result["all_checks_passed"] is False


class TestRunDoctorJson:
    def test_json_shape(self) -> None:
        with _patch_all_checks():
            result = run_doctor()
        dumped = json.dumps(result)
        parsed = json.loads(dumped)
        assert "schema_version" in parsed
        assert "all_checks_passed" in parsed
        assert isinstance(parsed["checks"], list)
        assert len(parsed["checks"]) == 10


# ---------------------------------------------------------------------------
# Repair: lock-held aborts
# ---------------------------------------------------------------------------


class TestRepairLockHeld:
    def test_lock_held_aborts_with_nonzero(self) -> None:
        result: dict[str, Any] = {
            "all_checks_passed": False,
            "checks": [
                {
                    "name": "ladybug_schema",
                    "passed": False,
                    "lock_held": True,
                    "remediation": "Stop the service and retry.",
                }
            ],
        }
        rc = _repair(result)
        assert rc != 0

    def test_lock_held_does_not_invoke_migrate(self) -> None:
        result: dict[str, Any] = {
            "all_checks_passed": False,
            "checks": [
                {
                    "name": "ladybug_schema",
                    "passed": False,
                    "lock_held": True,
                    "remediation": "Stop the service and retry.",
                }
            ],
        }
        with patch("agentalloy.storage.ladybug.LadybugStore") as mock_cls:
            _repair(result)
        mock_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Repair: schema-missing → migrate first, then install-packs
# ---------------------------------------------------------------------------


class TestRepairSchemaMissing:
    def test_migrate_called_before_install_packs(self) -> None:
        """Schema-missing (not lock-held) triggers migrate → install-packs order."""
        result: dict[str, Any] = {
            "all_checks_passed": False,
            "checks": [
                {"name": "config", "passed": True},
                {"name": "embed_server", "passed": True},
                {"name": "corpus_files", "passed": True},  # files present
                {"name": "ladybug_schema", "passed": False, "lock_held": False},
                {"name": "corpus_count", "passed": False},
                {"name": "embedding_dim", "passed": True},
                {"name": "service", "passed": True},
                {"name": "pack_manifests", "passed": True},
            ],
        }
        call_order: list[str] = []

        mock_store = MagicMock()
        mock_store.__enter__ = MagicMock(return_value=mock_store)
        mock_store.__exit__ = MagicMock(return_value=False)
        mock_store.migrate.side_effect = lambda: call_order.append("migrate")

        mock_sub = MagicMock(returncode=0)

        def fake_subprocess_run(cmd: Any, **_: Any) -> Any:
            call_order.append("install-packs")
            return mock_sub

        def fake_reembed(args: Any) -> int:
            call_order.append("reembed")
            return 0

        with (
            patch("agentalloy.storage.ladybug.LadybugStore", return_value=mock_store),
            patch("agentalloy.config.get_settings"),
            patch("subprocess.run", side_effect=fake_subprocess_run),
            # _repair imports reembed's main function-locally; patch at source
            # so the real reembed (host-dependent: embed server, corpus) never
            # runs inside a unit test.
            patch("agentalloy.reembed.cli.main", side_effect=fake_reembed),
            patch(
                "agentalloy.install.subcommands.doctor.run_doctor",
                return_value={
                    "all_checks_passed": True,
                    "checks": [{"name": "config", "passed": True}],
                },
            ),
            patch("agentalloy.install.subcommands.doctor._render_human_result"),
        ):
            rc = _repair(result)

        assert call_order == ["migrate", "install-packs", "reembed"]
        assert rc == 0


# ---------------------------------------------------------------------------
# Repair: all-green → no-op
# ---------------------------------------------------------------------------


class TestRepairNoop:
    def test_all_green_returns_zero(self) -> None:
        result: dict[str, Any] = {
            "all_checks_passed": True,
            "checks": [{"name": "config", "passed": True}],
        }
        rc = _repair(result)
        assert rc == 0


# ---------------------------------------------------------------------------
# Stub-creation guard (host mode): absent corpus must not create empty DB files
# ---------------------------------------------------------------------------


class TestNoStubCreation:
    def test_ladybug_schema_absent_creates_no_stub(self, tmp_path: Path) -> None:
        ladybug = tmp_path / "ladybug"
        result = _check_ladybug_schema(str(ladybug))
        assert result["passed"] is False
        assert "absent" in result["error"].lower()
        assert not ladybug.exists()

    def test_corpus_count_absent_creates_no_stub(self, tmp_path: Path) -> None:
        ladybug = tmp_path / "ladybug"
        duckdb = tmp_path / "skills.duck"
        result = _check_corpus_count(str(ladybug), str(duckdb))
        assert result["passed"] is False
        assert "absent" in result["error"].lower()
        assert not ladybug.exists()
        assert not duckdb.exists()


# ---------------------------------------------------------------------------
# Container deployment: verify via /health + volume inspection (not delegation)
# ---------------------------------------------------------------------------


_CONTAINER_STATE = "agentalloy.install.subcommands.container_runtime._container_state"
_HEALTH = "agentalloy.install.subcommands.doctor._fetch_health"
_FILE_EXISTS = "agentalloy.install.subcommands.doctor._container_file_exists"
_READ_FILE = "agentalloy.install.subcommands.doctor._container_read_file"
_PACK_MANIFESTS = "agentalloy.install.subcommands.doctor._check_pack_manifests"
_ORPHANS = "agentalloy.install.subcommands.doctor._check_orphans"
_DIAG = "agentalloy.install.subcommands.doctor._fetch_diagnostics"


def _healthy_diag() -> dict[str, Any]:
    return {"skill_count": 30, "embedded_vector_count": 412, "embedding_dim": 768}


def _healthy_body() -> dict[str, Any]:
    return {
        "status": "healthy",
        "dependencies": {
            "runtime_store": {"status": "ok"},
            "telemetry_store": {"status": "ok"},
            "embedding_runtime": {"status": "ok"},
            "runtime_cache": {"status": "ok"},
        },
    }


def _stamp() -> str:
    return json.dumps(
        {
            "embedding_model": "nomic-embed-text-v1.5.Q8_0.gguf",
            "embedding_dim": 768,
            "built_at": "2026-06-23T10:03:24+00:00",
        }
    )


_CONTAINER_IMAGE = "agentalloy.install.subcommands.doctor._container_image"


class TestContainerDoctor:
    _ST = {
        "deployment": "container",
        "runtime_binary": "podman",
        "container_name": "agentalloy",
        "image_tag": "ghcr.io/nrmeyers/agentalloy:latest",
        "port": 47950,
    }

    @pytest.fixture(autouse=True)
    def _patch_container_image(self) -> Any:
        # _run_doctor_container inspects the live image; pin it so tests don't
        # shell out to a real runtime.
        with patch(_CONTAINER_IMAGE, return_value="ghcr.io/nrmeyers/agentalloy:3.2.3"):
            yield

    def _healthy_patches(self) -> Any:
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(patch(_CONTAINER_STATE, return_value="running"))
        stack.enter_context(patch(_HEALTH, return_value=_healthy_body()))
        stack.enter_context(patch(_FILE_EXISTS, return_value=True))
        stack.enter_context(patch(_READ_FILE, return_value=_stamp()))
        stack.enter_context(
            patch(_PACK_MANIFESTS, return_value={"name": "pack_manifests", "passed": True})
        )
        stack.enter_context(patch(_ORPHANS, return_value={"name": "orphans", "passed": True}))
        stack.enter_context(patch(_DIAG, return_value=_healthy_diag()))
        return stack

    def test_healthy_container_all_pass(self) -> None:
        with self._healthy_patches():
            result = _run_doctor_container(self._ST)
        assert result["all_checks_passed"] is True
        names = {c["name"] for c in result["checks"]}
        assert {
            "container",
            "service",
            "embed_runtime",
            "corpus_files",
            "corpus_stamp",
            "corpus_count",
        } <= names
        corpus_count = next(c for c in result["checks"] if c["name"] == "corpus_count")
        assert corpus_count["passed"] is True

    def test_container_check_reports_live_image_not_stale_state(self) -> None:
        # state pins :latest but the container runs :3.2.3 — doctor must show the
        # live image, not the stale state field.
        with self._healthy_patches():
            result = _run_doctor_container(self._ST)
        container = next(c for c in result["checks"] if c["name"] == "container")
        assert "3.2.3" in container["detail"]
        assert ":latest" not in container["detail"]

    def test_run_doctor_routes_to_container(self) -> None:
        with self._healthy_patches():
            with patch(
                "agentalloy.install.subcommands.doctor.install_state.load_state",
                return_value=self._ST,
            ):
                result = run_doctor()
        assert result["all_checks_passed"] is True

    def test_not_running_fails_clean(self) -> None:
        with patch(_CONTAINER_STATE, return_value="exited"):
            result = _run_doctor_container(self._ST)
        assert result["all_checks_passed"] is False
        assert result["checks"][0]["name"] == "container"
        assert "not running" in result["checks"][0]["error"]

    def test_unreachable_service_fails(self) -> None:
        with (
            patch(_CONTAINER_STATE, return_value="running"),
            patch(_HEALTH, return_value=None),
            patch(_FILE_EXISTS, return_value=True),
            patch(_READ_FILE, return_value=_stamp()),
            patch(_PACK_MANIFESTS, return_value={"name": "pack_manifests", "passed": True}),
            patch(_ORPHANS, return_value={"name": "orphans", "passed": True}),
            patch(_DIAG, return_value=_healthy_diag()),
        ):
            result = _run_doctor_container(self._ST)
        assert result["all_checks_passed"] is False
        svc = next(c for c in result["checks"] if c["name"] == "service")
        assert svc["passed"] is False

    def test_missing_corpus_fails(self) -> None:
        with (
            patch(_CONTAINER_STATE, return_value="running"),
            patch(_HEALTH, return_value=_healthy_body()),
            patch(_FILE_EXISTS, return_value=False),
            patch(_READ_FILE, return_value=None),
            patch(_PACK_MANIFESTS, return_value={"name": "pack_manifests", "passed": True}),
            patch(_ORPHANS, return_value={"name": "orphans", "passed": True}),
            patch(_DIAG, return_value=_healthy_diag()),
        ):
            result = _run_doctor_container(self._ST)
        assert result["all_checks_passed"] is False
        corpus = next(c for c in result["checks"] if c["name"] == "corpus_files")
        assert corpus["passed"] is False

    def test_degraded_embed_runtime_fails(self) -> None:
        body = _healthy_body()
        body["dependencies"]["embedding_runtime"] = {"status": "down"}
        with (
            patch(_CONTAINER_STATE, return_value="running"),
            patch(_HEALTH, return_value=body),
            patch(_FILE_EXISTS, return_value=True),
            patch(_READ_FILE, return_value=_stamp()),
            patch(_PACK_MANIFESTS, return_value={"name": "pack_manifests", "passed": True}),
            patch(_ORPHANS, return_value={"name": "orphans", "passed": True}),
            patch(_DIAG, return_value=_healthy_diag()),
        ):
            result = _run_doctor_container(self._ST)
        assert result["all_checks_passed"] is False
        embed = next(c for c in result["checks"] if c["name"] == "embed_runtime")
        assert embed["passed"] is False

    def test_low_corpus_count_fails(self) -> None:
        with (
            patch(_CONTAINER_STATE, return_value="running"),
            patch(_HEALTH, return_value=_healthy_body()),
            patch(_FILE_EXISTS, return_value=True),
            patch(_READ_FILE, return_value=_stamp()),
            patch(_PACK_MANIFESTS, return_value={"name": "pack_manifests", "passed": True}),
            patch(_ORPHANS, return_value={"name": "orphans", "passed": True}),
            patch(
                _DIAG,
                return_value={
                    "skill_count": 3,
                    "embedded_vector_count": 10,
                    "embedding_dim": 768,
                },
            ),
        ):
            result = _run_doctor_container(self._ST)
        assert result["all_checks_passed"] is False
        cc = next(c for c in result["checks"] if c["name"] == "corpus_count")
        assert cc["passed"] is False
        assert "skill_count" in cc["error"]

    def test_zero_vectors_fails(self) -> None:
        with (
            patch(_CONTAINER_STATE, return_value="running"),
            patch(_HEALTH, return_value=_healthy_body()),
            patch(_FILE_EXISTS, return_value=True),
            patch(_READ_FILE, return_value=_stamp()),
            patch(_PACK_MANIFESTS, return_value={"name": "pack_manifests", "passed": True}),
            patch(_ORPHANS, return_value={"name": "orphans", "passed": True}),
            patch(
                _DIAG,
                return_value={
                    "skill_count": 30,
                    "embedded_vector_count": 0,
                    "embedding_dim": 768,
                },
            ),
        ):
            result = _run_doctor_container(self._ST)
        assert result["all_checks_passed"] is False
        cc = next(c for c in result["checks"] if c["name"] == "corpus_count")
        assert cc["passed"] is False
        assert "embedded_vector_count" in cc["error"]

    def test_diagnostics_endpoint_404_warns_not_fails(self) -> None:
        # Older image (version skew): /diagnostics/corpus 404s → _fetch_diagnostics
        # returns None. corpus_count must DEGRADE to a warn-pass, not hard-fail.
        with (
            patch(_CONTAINER_STATE, return_value="running"),
            patch(_HEALTH, return_value=_healthy_body()),
            patch(_FILE_EXISTS, return_value=True),
            patch(_READ_FILE, return_value=_stamp()),
            patch(_PACK_MANIFESTS, return_value={"name": "pack_manifests", "passed": True}),
            patch(_ORPHANS, return_value={"name": "orphans", "passed": True}),
            patch(_DIAG, return_value=None),
        ):
            result = _run_doctor_container(self._ST)
        assert result["all_checks_passed"] is True
        cc = next(c for c in result["checks"] if c["name"] == "corpus_count")
        assert cc["passed"] is True
        assert cc["severity"] == "warn"
        assert "unavailable" in cc["detail"]

    def test_repair_does_not_touch_host(self) -> None:
        """Container repair must NOT run host install-packs/reembed; it advises recreate."""
        result = {
            "all_checks_passed": False,
            "checks": [{"name": "corpus_files", "passed": False, "error": "x"}],
        }
        with patch("subprocess.run") as mock_run:
            rc = _repair_container(result, self._ST)
        assert rc == 1
        mock_run.assert_not_called()
