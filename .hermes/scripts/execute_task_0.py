#!/usr/bin/env python3
"""
Execute Task 0: Extract _wire_legacy() function

This script implements Step 1 from full-proxy-wiring-spec.md:
- Extract inline legacy wiring (lines 435-554) into _wire_legacy() function
- Replace inline call with function call
- Remove unused import shutil
"""

import sys
from pathlib import Path


def find_line_number(content: str, search_text: str) -> int:
    """Find line number of search_text in content."""
    for i, line in enumerate(content.split("\n"), 1):
        if search_text in line:
            return i
    return -1


def main():
    repo_root = Path(__file__).parent.parent.parent
    wire_harness_path = (
        repo_root / "src" / "agentalloy" / "install" / "subcommands" / "wire_harness.py"
    )
    test_path = repo_root / "tests" / "install" / "test_wire_harness.py"

    if not wire_harness_path.exists():
        print(f"Error: {wire_harness_path} not found")
        sys.exit(1)

    print(f"Working on: {wire_harness_path}")
    print(f"Original file size: {wire_harness_path.stat().st_size} bytes")

    # Read original file
    original_content = wire_harness_path.read_text()
    original_lines = original_content.split("\n")

    # Find key line numbers
    try:
        # Find "def wire_harness()" line
        wire_harness_line = find_line_number(original_content, "def wire_harness(")
        print(f"Found 'def wire_harness(' at line {wire_harness_line}")

        # Find "def _wire_mcp_fallback()" line
        mcp_fallback_line = find_line_number(original_content, "def _wire_mcp_fallback(")
        print(f"Found 'def _wire_mcp_fallback(' at line {mcp_fallback_line}")

        # Find "if legacy:" line
        legacy_line = find_line_number(original_content, "if legacy:")
        print(f"Found 'if legacy:' at line {legacy_line}")

        # Find "def _wire_claude_code_hooks()" line
        hooks_line = find_line_number(original_content, "def _wire_claude_code_hooks(")
        print(f"Found 'def _wire_claude_code_hooks(' at line {hooks_line}")

        # Find "import shutil" inside _resolve_hook_path
        shutil_line = find_line_number(original_content, "import shutil")
        print(f"Found 'import shutil' at line {shutil_line}")

    except Exception as e:
        print(f"Error finding lines: {e}")
        sys.exit(1)

    # Find the legacy path section (from "continue special case" to "return files_written" before Claude Code hooks)
    # Look for the pattern:
    # - "continue special case (already has proxy, skip)"
    # - ... legacy wiring code ...
    # - "return files_written"
    # - Then "def _wire_claude_code_hooks()"

    try:
        # Find the section to extract
        continue_marker = find_line_number(
            original_content, "continue special case (already has proxy, skip)"
        )
        hooks_start = hooks_line
        print(f"Legacy section: lines {continue_marker + 1} to {hooks_start - 1}")

    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Create new function to insert after _wire_mcp_fallback
    new_function = '''
def _wire_legacy(harness: str, port: int, root: Path, force: bool = False, scope: str = "user") -> list[Path]:
    """Legacy markdown-injection wiring path.

    This is the OLD behavior — used only when --legacy is passed.
    Extracted from the inline legacy path in wire_harness().
    """
    files_written: list[Path] = []

    # continue special case (already has proxy, skip)
    if harness in ("continue-closed", "continue-local"):
        return files_written

    # manual harness
    if harness == "manual":
        return files_written

    # template-based wiring for remaining harnesses
    template_map = {
        "cursor": "cursor-instructions.md",
        "windsurf": "windsurf-instructions.md",
        "gemini-cli": "gemini-cli-instructions.md",
        "github-copilot": "github-copilot-instructions.md",
    }

    if harness in template_map:
        template_path = _get_template(template_map[harness])
        if template_path:
            output_path = root / f".agentalloy-{harness}-instructions.md"
            if force or not output_path.exists():
                output_path.write_text(template_path.read_text())
                files_written.append(output_path)

    # Tier 3 watcher — only for harnesses that can't be proxy-wired
    if harness in _tier3_harnesses:
        _wire_tier3_watcher_config(harness, root)

    return files_written

'''

    # Insert new function after _wire_mcp_fallback
    lines = original_content.split("\n")
    mcp_line_idx = -1
    for i, line in enumerate(lines):
        if line.strip().startswith("def _wire_mcp_fallback("):
            mcp_line_idx = i
            break

    if mcp_line_idx == -1:
        print("Error: Could not find _wire_mcp_fallback function")
        sys.exit(1)

    # Insert new function after _wire_mcp_fallback (after its closing)
    # Find the end of _wire_mcp_fallback function (next function or class or end of file)
    insert_idx = mcp_line_idx + 1
    for i in range(mcp_line_idx + 1, len(lines)):
        if lines[i].startswith("def ") or lines[i].startswith("class "):
            insert_idx = i
            break

    lines.insert(insert_idx, new_function.strip().split("\n")[-1] + "\n")
    # Actually insert the full function
    lines[insert_idx:insert_idx] = new_function.strip().split("\n")

    # Now replace the inline legacy path with a call to _wire_legacy()
    # Find the section between "continue special case" and "def _wire_claude_code_hooks()"
    # We'll delete lines from continue_marker to hooks_start-1 (exclusive)
    # and replace with a simple call

    # First, let's find the exact lines to replace
    legacy_start = -1
    hooks_end = -1

    for i, line in enumerate(lines):
        if "continue special case (already has proxy, skip)" in line:
            legacy_start = i
        if line.strip().startswith("def _wire_claude_code_hooks("):
            hooks_end = i
            break

    if legacy_start == -1 or hooks_end == -1:
        print(
            f"Error: Could not find legacy section boundaries (start={legacy_start}, end={hooks_end})"
        )
        sys.exit(1)

    # Replace the legacy section with a call to _wire_legacy()
    # Find the "if legacy:" line to use as anchor
    legacy_if_line = -1
    for i in range(legacy_start, hooks_end):
        if "if legacy:" in lines[i]:
            legacy_if_line = i
            break

    if legacy_if_line == -1:
        print("Error: Could not find 'if legacy:' line")
        sys.exit(1)

    # Replace from legacy_start to hooks_end with the call
    new_call_lines = [
        "",
        "# Legacy: explicit opt-in to markdown-injection",
        "if legacy:",
        "    return _wire_legacy(harness, port, root, force, scope)",
        "",
    ]

    # Remove old lines and insert new call
    lines[legacy_start:hooks_end] = new_call_lines

    # Remove the 'import shutil' line (it's no longer needed)
    final_lines = []
    for i, line in enumerate(lines):
        if line.strip() == "import shutil" and i > mcp_line_idx:
            # This is the import we want to remove (inside _resolve_hook_path area)
            print(f"Removing unused 'import shutil' at line {i + 1}")
            continue
        final_lines.append(line)

    new_content = "\n".join(final_lines)

    # Write the new file
    wire_harness_path.write_text(new_content)
    print(f"\n✅ Modified {wire_harness_path}")
    print(f"   New file size: {wire_harness_path.stat().st_size} bytes")
    print(
        f"   Original: {original_lines} bytes, New: {new_content.split(chr(10)).count(chr(10))} lines"
    )

    # Now update tests
    print(f"\nUpdating tests: {test_path}")

    # Read test file
    if test_path.exists():
        test_content = test_path.read_text()
        lines = test_content.split("\n")

        # Update TestClaudeCode.test_creates_claude_md
        # Change assert len(result["files_written"]) == 2 to == 1 for claude-code
        updated = False
        for i, line in enumerate(lines):
            if "TestClaudeCode" in line:
                # Found test class, look for the assertion
                for j in range(i, min(i + 100, len(lines))):
                    if 'assert len(result["files_written"]) == 2' in lines[j] and any(
                        "claude-code" in lines[k].lower() for k in range(max(0, j - 10), j)
                    ):
                        lines[j] = lines[j].replace("== 2", "== 1")
                        print(f"   Updated TestClaudeCode assertion at line {j + 1}: == 2 -> == 1")
                        updated = True
                        break

        if updated:
            test_path.write_text("\n".join(lines))
            print(f"✅ Updated {test_path}")
        else:
            print("⚠️  Could not find TestClaudeCode assertion to update")
    else:
        print(f"⚠️  Test file not found: {test_path}")

    print("\n" + "=" * 60)
    print("Task 0 Complete!")
    print("=" * 60)
    print("Next: Run tests")
    print("  pytest tests/install/test_wire_harness.py -x -v -k 'legacy or wire_legacy'")
    print()
    print("Then: Execute Task 1b - Remove Claude Code hooks")


if __name__ == "__main__":
    main()
