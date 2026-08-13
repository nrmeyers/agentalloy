"""Tests for JSX artifact stripping in authoring-agent skill YAML files.

Phase 3 (JSX Stripping) — Batch A targets four skills with complex JSX:
  - linting/linting-typescript-eslint.yaml      (::note, :::caution, <Tabs>, <TabItem>)
  - ui-design/ui-design-states-and-variants.yaml (<Figure>, <Example>, <TipBad>, <TipGood>)
  - rest/rest-versioning-and-compatibility.yaml  (already clean)
  - nextjs/nextjs-csp-and-security-headers.yaml  (already clean)

Batch B targets three skills with simpler JSX:
  - nextjs/nextjs-isr-and-revalidation.yaml
  - data-engineering/data-engineering-dbt-incremental.yaml
  - webhooks/webhooks-idempotency.yaml

JSX artifacts are Docusaurus/Radix component tags that must be converted
to plain Markdown per the corpus reduction spec (section 4.4). The spec
verifies zero matches for: <Callout>, <Term>, <CodeTabs>, <details>,
<TabItem>, <import>, <CopilotBeta>. Standard HTML elements (lowercase,
except <details> which is a Docusaurus-pattern usage) and code inside
fenced code blocks are not considered artifacts.
"""

from __future__ import annotations

from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKS_DIR = REPO_ROOT / "src" / "agentalloy" / "_packs"

# JSX component types that are NOT standard HTML (these are the artifacts
# per the corpus reduction spec section 4.4 verification step).
JSX_COMPONENTS = {
    "Callout",
    "CodeTabs",
    "TabItem",
    "Term",
    "details",
    "import",
    "CopilotBeta",
}

# Files in Batch A
BATCH_A_FILES = [
    PACKS_DIR / "linting" / "linting-typescript-eslint.yaml",
    PACKS_DIR / "ui-design" / "ui-design-states-and-variants.yaml",
    PACKS_DIR / "rest" / "rest-versioning-and-compatibility.yaml",
    PACKS_DIR / "nextjs" / "nextjs-csp-and-security-headers.yaml",
]

# Files in Batch B
BATCH_B_FILES = [
    PACKS_DIR / "nextjs" / "nextjs-isr-and-revalidation.yaml",
    PACKS_DIR / "data-engineering" / "data-engineering-dbt-incremental.yaml",
    PACKS_DIR / "webhooks" / "webhooks-idempotency.yaml",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_jsx_tags(content: str) -> list[str]:
    """Return all JSX-like tags found in *content*.

    Skips tags that appear inside fenced code blocks (``` ... ```).
    Handles indented code fences found in YAML literal block scalars.
    """
    import re

    lines = content.split("\n")
    jsx_re = re.compile(r"</?[A-Za-z][a-zA-Z0-9]*(?:\s+[^>]*)?/?>")

    # Track code block depth using a stack-based approach
    # In YAML content: ``` (just backticks) toggles depth,
    # ```language is always an opening fence
    code_depth = 0
    prose_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            rest = stripped[3:]
            rest_stripped = rest.strip()
            has_language = bool(re.match(r"[a-zA-Z]", rest_stripped))
            if has_language:
                code_depth += 1
            else:
                if code_depth > 0:
                    code_depth -= 1
                else:
                    code_depth += 1
            continue
        if code_depth == 0:
            prose_lines.append(line)

    prose = "\n".join(prose_lines)
    return jsx_re.findall(prose)


def _check_file(filepath: Path) -> list[str]:
    """Check a single YAML file for JSX artifacts. Returns list of hits."""
    content = filepath.read_text(encoding="utf-8")
    tags = _extract_jsx_tags(content)

    # Filter to only JSX component tags (not standard HTML like <details>).
    jsx_components = [tag for tag in tags if tag.split()[0].lstrip("<") in JSX_COMPONENTS]

    # Also check for Docusaurus-style callout markers in prose
    import re

    for m in re.finditer(r"::note|:::caution|:::warning", content):
        # Verify this match is outside code blocks
        pos = m.start()
        lines_before = content[:pos].split("\n")
        code_depth = 0
        for line in lines_before[:-1]:
            stripped = line.strip()
            if stripped.startswith("```"):
                rest = stripped[3:]
                has_lang = bool(re.match(r"[a-zA-Z]", rest.strip()))
                if has_lang:
                    code_depth += 1
                else:
                    if code_depth > 0:
                        code_depth -= 1
                    else:
                        code_depth += 1
        if code_depth == 0:
            jsx_components.append(m.group(0))

    return jsx_components


# ---------------------------------------------------------------------------
# Batch A: complex JSX no-JSX check
# ---------------------------------------------------------------------------


def test_batch_a_complex_jsx_no_jsx() -> None:
    """All four Batch A skill files must be free of JSX component artifacts.

    Acceptance criteria:
      - Zero JSX artifacts in prose (outside code blocks).
      - Converted Markdown renders correctly (YAML parses cleanly).
      - All 4 skills ingest cleanly.
    """
    for filepath in BATCH_A_FILES:
        assert filepath.exists(), f"Skill file missing: {filepath}"

        # Verify YAML parses cleanly
        data = yaml.safe_load(filepath.read_text(encoding="utf-8"))
        assert isinstance(data, dict), f"{filepath.name} is not a valid YAML dict"

        # Check for JSX artifacts
        jsx_components = _check_file(filepath)

        assert not jsx_components, f"{filepath.name} still contains JSX artifacts: {jsx_components}"


def test_batch_a_raw_prose_has_no_jsx() -> None:
    """Raw prose sections of Batch A YAML files must contain no JSX artifacts.

    This test loads each YAML, extracts the raw_prose field and each
    fragment's content field, and verifies no JSX-like tags remain.
    """
    import re

    for filepath in BATCH_A_FILES:
        data = yaml.safe_load(filepath.read_text(encoding="utf-8"))

        # Check top-level raw_prose
        raw = data.get("raw_prose", "")
        if raw:
            tags = re.findall(r"</?[A-Za-z][a-zA-Z0-9]*(?:\s+[^>]*)?/?>", raw)
            jsx = [t for t in tags if t.split()[0].lstrip("<") in JSX_COMPONENTS]
            assert not jsx, f"{filepath.name}: JSX in top-level raw_prose: {jsx}"
            docusaurus = re.findall(r"::note|:::caution|:::warning", raw)
            assert not docusaurus, f"{filepath.name}: Docusaurus markers in raw_prose: {docusaurus}"

        # Check fragment content
        fragments = data.get("fragments", [])
        for frag in fragments:
            content = frag.get("content", "")
            if not content:
                continue
            tags = re.findall(r"<[A-Z][a-zA-Z0-9]*(?:\s+[^>]*)?/?>", content)
            jsx = [t for t in tags if t.split()[0].lstrip("<") in JSX_COMPONENTS]
            assert not jsx, (
                f"{filepath.name} fragment sequence {frag.get('sequence')}: JSX in content: {jsx}"
            )
            docusaurus = re.findall(r"::note|:::caution|:::warning", content)
            assert not docusaurus, (
                f"{filepath.name} fragment sequence {frag.get('sequence')}: "
                f"Docusaurus markers in content: {docusaurus}"
            )


# ---------------------------------------------------------------------------
# Batch B: simple JSX no-JSX check
# ---------------------------------------------------------------------------


def test_batch_b_simple_jsx_no_jsx() -> None:
    """All three Batch B skill files must be free of JSX component artifacts.

    Acceptance criteria:
      - Zero JSX artifacts in prose (outside code blocks).
      - Converted Markdown renders correctly (YAML parses cleanly).
      - All 3 skills ingest cleanly.
    """
    for filepath in BATCH_B_FILES:
        assert filepath.exists(), f"Skill file missing: {filepath}"

        # Verify YAML parses cleanly
        data = yaml.safe_load(filepath.read_text(encoding="utf-8"))
        assert isinstance(data, dict), f"{filepath.name} is not a valid YAML dict"

        # Check for JSX artifacts
        jsx_components = _check_file(filepath)

        assert not jsx_components, f"{filepath.name} still contains JSX artifacts: {jsx_components}"
