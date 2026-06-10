"""Unit tests for the ``install-pack`` subcommand.

Network and subprocess paths are mocked; the focus is on the contract +
security surfaces (URL scheme allowlist, pack-name validation, manifest
field validation, sha256 mismatch handling, tarball safety).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agentalloy.install.subcommands import install_pack as ip


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("")
    return tmp_path


class TestPackNameValidation:
    def test_valid_pack_name_resolves_to_default_url(self) -> None:
        url = ip._resolve_manifest_url("frontend", None)  # pyright: ignore[reportPrivateUsage]
        assert "skill-pack-frontend" in url
        assert url.startswith("https://")

    def test_path_traversal_in_pack_name_rejected(self) -> None:
        with pytest.raises(SystemExit):
            ip._resolve_manifest_url("../../etc/passwd", None)  # pyright: ignore[reportPrivateUsage]

    def test_slash_in_pack_name_rejected(self) -> None:
        with pytest.raises(SystemExit):
            ip._resolve_manifest_url("evil/pack", None)  # pyright: ignore[reportPrivateUsage]

    def test_scheme_injection_in_pack_name_rejected(self) -> None:
        with pytest.raises(SystemExit):
            ip._resolve_manifest_url("https://attacker.com/x?p", None)  # pyright: ignore[reportPrivateUsage]

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(SystemExit):
            ip._resolve_manifest_url("", None)  # pyright: ignore[reportPrivateUsage]


class TestUrlSchemeAllowlist:
    def test_file_scheme_rejected_via_override(self) -> None:
        with pytest.raises(SystemExit):
            ip._validate_url("file:///etc/passwd", "manifest")  # pyright: ignore[reportPrivateUsage]

    def test_ftp_scheme_rejected(self) -> None:
        with pytest.raises(SystemExit):
            ip._validate_url("ftp://example.com/x", "manifest")  # pyright: ignore[reportPrivateUsage]

    def test_https_allowed(self) -> None:
        ip._validate_url("https://example.com/manifest.json", "manifest")  # pyright: ignore[reportPrivateUsage]

    def test_http_allowed(self) -> None:
        ip._validate_url("http://localhost:8080/x", "manifest")  # pyright: ignore[reportPrivateUsage]


class TestManifestValidation:
    def test_manifest_fetch_failure_returns_structured(self, repo_root: Path) -> None:
        from urllib.error import URLError

        with patch.object(ip, "_download", side_effect=URLError("dns failure")):
            result = ip.install_pack("frontend", root=repo_root)
        assert result["action"] == "manifest_fetch_failed"
        assert "remediation" in result

    def test_manifest_missing_required_fields(self, repo_root: Path, tmp_path: Path) -> None:
        # Stub _download to write a manifest missing `tarball_url`
        def fake_download(url: str, dest: Path, max_bytes: int, timeout: int = 60) -> None:
            dest.write_text(json.dumps({"sha256": "x" * 64}))

        with patch.object(ip, "_download", side_effect=fake_download):
            result = ip.install_pack("frontend", root=repo_root)
        assert result["action"] == "manifest_invalid"


class TestSha256Mismatch:
    def test_sha_mismatch_aborts(self, repo_root: Path) -> None:
        manifest = {"tarball_url": "https://example.com/p.tar.gz", "sha256": "0" * 64}
        # First _download call writes manifest; second writes tarball.
        call_count = {"n": 0}

        def fake_download(url: str, dest: Path, max_bytes: int, timeout: int = 60) -> None:
            call_count["n"] += 1
            if call_count["n"] == 1:
                dest.write_text(json.dumps(manifest))
            else:
                # Tarball content with a different sha than the manifest claims
                dest.write_bytes(b"not the expected bytes")

        with patch.object(ip, "_download", side_effect=fake_download):
            result = ip.install_pack("frontend", root=repo_root)
        assert result["action"] == "sha256_mismatch"
        assert "expected_sha256" in result and "actual_sha256" in result


class TestSizeCaps:
    def test_size_caps_constants_sane(self) -> None:
        # Manifest cap is small (it's just JSON metadata), tarball larger.
        assert ip._MAX_MANIFEST_BYTES <= 4 << 20  # pyright: ignore[reportPrivateUsage]
        assert ip._MAX_TARBALL_BYTES >= 16 << 20  # pyright: ignore[reportPrivateUsage]
        assert ip._MAX_TARBALL_BYTES < 1 << 30  # pyright: ignore[reportPrivateUsage]

    def test_oversize_payload_aborts(self, repo_root: Path) -> None:
        # The download helper raises RuntimeError when it exceeds max_bytes.
        # We exercise it indirectly by feeding a fake response that's too big.
        from unittest.mock import MagicMock

        big_payload = b"x" * (ip._MAX_MANIFEST_BYTES + 1)  # pyright: ignore[reportPrivateUsage]
        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.read.side_effect = [big_payload[:65536], b""]
        fake_resp.__enter__ = lambda s: s  # pyright: ignore[reportUnknownLambdaType]
        fake_resp.__exit__ = lambda *a: None  # pyright: ignore[reportUnknownLambdaType]
        # Make read() return the whole oversize payload in one chunk
        chunks = [big_payload[i : i + 65536] for i in range(0, len(big_payload), 65536)] + [b""]
        fake_resp.read.side_effect = chunks
        with (
            patch("urllib.request.urlopen", return_value=fake_resp),
            pytest.raises(RuntimeError, match="exceeded"),
        ):
            ip._download(  # pyright: ignore[reportPrivateUsage]
                "https://example.com/x",
                Path(repo_root) / "out",
                max_bytes=ip._MAX_MANIFEST_BYTES,  # pyright: ignore[reportPrivateUsage]
            )


class TestRenderHumanFailureDetail:
    """Issue #84: "Failures: N" alone gave zero diagnostic signal — the
    human renderer must surface the first failing skill's yaml + stderr."""

    @staticmethod
    def _result(ingest_results: list[dict[str, object]], failures: int) -> dict[str, object]:
        return {
            "action": "ingested_with_errors",
            "pack": "core",
            "skills_ingested": 0,
            "ingest_failures": failures,
            "ingest_results": ingest_results,
        }

    def test_first_failure_yaml_and_stderr_shown(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = self._result(
            [
                {"yaml": "ok.yaml", "outcome": "ingested", "stderr_tail": ""},
                {
                    "yaml": "writing-readmes.yaml",
                    "outcome": "failed",
                    "stderr_tail": "RuntimeError: Binder exception: Table Skill does not exist.",
                },
                {"yaml": "later.yaml", "outcome": "failed", "stderr_tail": "boom"},
            ],
            failures=4,
        )
        ip._render_human(result)  # pyright: ignore[reportPrivateUsage]
        out = capsys.readouterr().out
        assert "Failures: 4" in out
        assert "writing-readmes.yaml" in out
        assert "Table Skill does not exist" in out
        # Only the FIRST failure is surfaced inline.
        assert "later.yaml" not in out

    def test_long_stderr_tail_is_truncated(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = self._result(
            [{"yaml": "big.yaml", "outcome": "failed", "stderr_tail": "E" * 500}],
            failures=1,
        )
        ip._render_human(result)  # pyright: ignore[reportPrivateUsage]
        out = capsys.readouterr().out
        assert "big.yaml" in out
        assert "E" * 117 + "..." in out
        assert "E" * 200 not in out

    def test_missing_ingest_results_still_prints_count(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # install-packs strips ingest_results from some payloads; the
        # renderer must not crash and must keep the bare count.
        result = self._result([], failures=3)
        del result["ingest_results"]
        ip._render_human(result)  # pyright: ignore[reportPrivateUsage]
        out = capsys.readouterr().out
        assert "Failures: 3" in out

    def test_lock_held_failure_prints_remediation(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = self._result(
            [
                {
                    "yaml": "first.yaml",
                    "outcome": "failed",
                    "stderr_tail": (
                        "RuntimeError: IO exception: Could not set lock on file "
                        "ladybug.lock: Lock is held by PID 12345"
                    ),
                }
            ],
            failures=1,
        )
        ip._render_human(result)  # pyright: ignore[reportPrivateUsage]
        out = capsys.readouterr().out
        assert "Another process holds the corpus DB lock" in out
        assert "agentalloy server-stop" in out
