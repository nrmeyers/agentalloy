"""Tests for phase discipline enforcement (tool call interception)."""

from agentalloy.providers.base import (
    _command_advances_phase_without_approval,
    _command_writes_to_code,
    build_instructive_denial_message,
    intercept_gated_tool_calls,
)


class TestCommandWritesToCode:
    """Tests for _command_writes_to_code."""

    def test_redirect_to_src(self):
        assert _command_writes_to_code("echo 'hello' > src/main.py")

    def test_redirect_to_tests(self):
        assert _command_writes_to_code("echo 'test' > tests/test_main.py")

    def test_append_to_src(self):
        assert _command_writes_to_code("echo 'code' >> src/utils.py")

    def test_tee_to_src(self):
        assert _command_writes_to_code("cat file | tee src/output.py")

    def test_cp_to_src(self):
        assert _command_writes_to_code("cp template.py src/new.py")

    def test_mv_to_tests(self):
        assert _command_writes_to_code("mv old.py tests/new.py")

    def test_redirect_to_docs_allowed(self):
        assert not _command_writes_to_code("echo 'doc' > docs/readme.md")

    def test_read_from_src_allowed(self):
        assert not _command_writes_to_code("cat src/main.py")

    def test_grep_allowed(self):
        assert not _command_writes_to_code("grep -r 'pattern' src/")


class TestCommandAdvancesPhaseWithoutApproval:
    """Tests for _command_advances_phase_without_approval."""

    def test_spec_phase_blocked(self):
        assert _command_advances_phase_without_approval("agentalloy phase set design", "spec")

    def test_design_phase_blocked(self):
        assert _command_advances_phase_without_approval("agentalloy phase set plan", "design")

    def test_plan_phase_blocked(self):
        assert _command_advances_phase_without_approval("agentalloy phase set build", "plan")

    def test_build_phase_allowed(self):
        # build phase doesn't require approval
        assert not _command_advances_phase_without_approval("agentalloy phase set qa", "build")

    def test_intake_phase_allowed(self):
        # intake phase doesn't require approval
        assert not _command_advances_phase_without_approval("agentalloy phase set spec", "intake")

    def test_non_phase_command_allowed(self):
        assert not _command_advances_phase_without_approval("agentalloy code index", "spec")


class TestBuildInstructiveDenialMessage:
    """Tests for build_instructive_denial_message."""

    def test_includes_phase(self):
        msg = build_instructive_denial_message("spec", "write_file")
        assert "spec" in msg

    def test_includes_tool_name(self):
        msg = build_instructive_denial_message("spec", "write_file")
        assert "write_file" in msg

    def test_includes_deliverable(self):
        msg = build_instructive_denial_message("spec", "write_file")
        assert "deliverable" in msg.lower() or "spec" in msg

    def test_approval_required_phases_mention_approve(self):
        for phase in ["spec", "design", "plan"]:
            msg = build_instructive_denial_message(phase, "write_file")
            assert "approve" in msg

    def test_non_approval_phases_dont_mention_approve(self):
        msg = build_instructive_denial_message("intake", "write_file")
        # intake doesn't require approval, so the message should be different
        assert "write_file" in msg


class TestInterceptGatedToolCalls:
    """Tests for intercept_gated_tool_calls."""

    def test_blocks_write_file_in_spec(self):
        blocks = [{"type": "tool_use", "name": "write_file", "id": "1", "input": {}}]
        result, modified = intercept_gated_tool_calls(blocks, "spec")
        assert modified
        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert "denied" in result[0]["text"].lower()

    def test_blocks_edit_in_design(self):
        blocks = [{"type": "tool_use", "name": "edit", "id": "1", "input": {}}]
        result, modified = intercept_gated_tool_calls(blocks, "design")
        assert modified
        assert result[0]["type"] == "text"

    def test_blocks_notebook_edit_in_intake(self):
        blocks = [{"type": "tool_use", "name": "notebook_edit", "id": "1", "input": {}}]
        result, modified = intercept_gated_tool_calls(blocks, "intake")
        assert modified
        assert result[0]["type"] == "text"

    def test_allows_write_file_in_build(self):
        blocks = [{"type": "tool_use", "name": "write_file", "id": "1", "input": {}}]
        result, modified = intercept_gated_tool_calls(blocks, "build")
        assert not modified
        assert result == blocks

    def test_allows_read_file_in_spec(self):
        blocks = [{"type": "tool_use", "name": "read_file", "id": "1", "input": {}}]
        result, modified = intercept_gated_tool_calls(blocks, "spec")
        assert not modified
        assert result == blocks

    def test_blocks_shell_write_in_spec(self):
        blocks = [
            {
                "type": "tool_use",
                "name": "run_shell_command",
                "id": "1",
                "input": {"command": "echo 'code' > src/main.py"},
            }
        ]
        result, modified = intercept_gated_tool_calls(blocks, "spec")
        assert modified
        assert result[0]["type"] == "text"
        assert "denied" in result[0]["text"].lower()

    def test_allows_shell_read_in_spec(self):
        blocks = [
            {
                "type": "tool_use",
                "name": "run_shell_command",
                "id": "1",
                "input": {"command": "cat src/main.py"},
            }
        ]
        result, modified = intercept_gated_tool_calls(blocks, "spec")
        assert not modified
        assert result == blocks

    def test_blocks_phase_advance_in_spec(self):
        blocks = [
            {
                "type": "tool_use",
                "name": "run_shell_command",
                "id": "1",
                "input": {"command": "agentalloy phase set design"},
            }
        ]
        result, modified = intercept_gated_tool_calls(blocks, "spec")
        assert modified
        assert result[0]["type"] == "text"
        assert "approval" in result[0]["text"].lower()

    def test_pause_mode_bypasses_all(self):
        blocks = [{"type": "tool_use", "name": "write_file", "id": "1", "input": {}}]
        result, modified = intercept_gated_tool_calls(blocks, "spec", pause_mode=True)
        assert not modified
        assert result == blocks

    def test_mixed_blocks(self):
        blocks = [
            {"type": "text", "text": "Let me write some code"},
            {"type": "tool_use", "name": "write_file", "id": "1", "input": {}},
            {"type": "tool_use", "name": "read_file", "id": "2", "input": {}},
        ]
        result, modified = intercept_gated_tool_calls(blocks, "spec")
        assert modified
        assert len(result) == 3
        assert result[0]["type"] == "text"  # original text preserved
        assert result[1]["type"] == "text"  # write_file replaced with denial
        assert result[2]["type"] == "tool_use"  # read_file preserved
