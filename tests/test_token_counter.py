"""Token counter tests — verify counting with /tokenize and fallbacks."""

from agentalloy.token_counter import TokenCounter


def test_count_empty_string() -> None:
    """Empty string returns 0."""
    counter = TokenCounter(model_url="http://localhost:99999")
    assert counter.count("") == 0


def test_count_via_model_server() -> None:
    """Count tokens using the live /tokenize endpoint."""
    counter = TokenCounter(
        model_url="http://localhost:50001",
        api_key="sk-local-f2d05be43df88a4c5b96b0915ec10029",
    )
    count = counter.count("hello world test")
    assert count == 3


def test_count_fallback_tiktoken() -> None:
    """When model server is unreachable, falls back to tiktoken."""
    counter = TokenCounter(model_url="http://localhost:99999")
    count = counter.count("hello world, this is a test of tiktoken fallback")
    assert count > 0
    assert isinstance(count, int)


def test_count_char_estimate() -> None:
    """When both /tokenize and tiktoken fail, uses char estimate."""
    counter = TokenCounter(model_url="http://localhost:99999")
    counter._tiktoken_tried = True
    counter._tiktoken_enc = None
    count = counter.count("a" * 100)
    assert count == 25  # 100 // 4


def test_count_steering_context() -> None:
    """Count tokens in a realistic steering context string."""
    counter = TokenCounter(
        model_url="http://localhost:50001",
        api_key="sk-local-f2d05be43df88a4c5b96b0915ec10029",
    )
    steering = (
        "# Current Phase: build\n\n"
        "# Active Contracts\n"
        "Phase: build\n\n"
        "## Skill: code-search\n"
        "Use code_search to find relevant code in the repository."
    )
    count = counter.count(steering)
    assert count > 0
    assert count < 200  # Sanity check — steering should be compact
