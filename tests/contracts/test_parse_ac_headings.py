"""Tests for parse_ac_headings in agentalloy.contracts.

Test cases from task 02-ac-parsing.md:

- AC-2.1: ## AC-1: login works → [{id: "AC-1", text: "login works"}]
- AC-2.2: ## Out of Scope → no match
- AC-2.3: ## AC-1: login works\n## AC-2: logout works\n## Out of Scope\n## AC-3: signup works
  → [{id: "AC-1", text: "login works"}, {id: "AC-2", text: "logout works"}, {id: "AC-3", text: "signup works"}]
"""

from agentalloy.contracts import parse_ac_headings


class TestParseAcHeadings:
    """parse_ac_headings extracts AC IDs and text from markdown headings."""

    def test_single_ac_heading(self) -> None:
        """AC-2.1: ## AC-1: login works → [{id: "AC-1", text: "login works"}]."""
        result = parse_ac_headings("## AC-1: login works")
        assert result == [{"id": "AC-1", "text": "login works"}]

    def test_non_ac_heading_no_match(self) -> None:
        """AC-2.2: ## Out of Scope → no match."""
        result = parse_ac_headings("## Out of Scope")
        assert result == []

    def test_multiple_ac_headings(self) -> None:
        """AC-2.3: multiple AC headings with non-AC headings in between."""
        body = "## AC-1: login works\n## AC-2: logout works\n## Out of Scope\n## AC-3: signup works"
        result = parse_ac_headings(body)
        assert len(result) == 3
        assert result[0] == {"id": "AC-1", "text": "login works"}
        assert result[1] == {"id": "AC-2", "text": "logout works"}
        assert result[2] == {"id": "AC-3", "text": "signup works"}

    def test_three_digit_ac_numbers(self) -> None:
        """AC IDs with multi-digit numbers are parsed correctly."""
        result = parse_ac_headings("## AC-10: big feature\n## AC-100: huge feature")
        assert result == [
            {"id": "AC-10", "text": "big feature"},
            {"id": "AC-100", "text": "huge feature"},
        ]

    def test_three_level_headings(self) -> None:
        """### AC-N: text also matches (3-hash headings)."""
        result = parse_ac_headings("### AC-5: nested heading")
        assert result == [{"id": "AC-5", "text": "nested heading"}]

    def test_ac_with_space_instead_of_colon(self) -> None:
        r"""AC headings with space after number (no colon) still match -- regex uses [\s:] ."""
        result = parse_ac_headings("## AC-1 login works")
        assert result == [{"id": "AC-1", "text": "login works"}]

    def test_empty_input(self) -> None:
        """Empty string returns empty list."""
        result = parse_ac_headings("")
        assert result == []

    def test_no_ac_headings(self) -> None:
        """Text with no AC headings returns empty list."""
        result = parse_ac_headings("# Project\n\nSome body text.\n## Other section")
        assert result == []
