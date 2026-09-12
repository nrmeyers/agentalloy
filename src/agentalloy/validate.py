"""Tool call validation: JSON-Schema arg check + index-grounded check.

Fail-open on store error (AC-13).
"""

import json
from typing import Any

from agentalloy.tools import ALL_TOOLS


def validate_tool_call(tool_name: str, args: str) -> tuple[bool, str | None]:
    """Validate a tool call.

    Args:
        tool_name: Name of the tool
        args: JSON string of arguments

    Returns:
        (is_valid, error_message)
    """
    # Parse args
    try:
        parsed_args = json.loads(args) if isinstance(args, str) else args
    except json.JSONDecodeError as e:
        return False, f"Invalid JSON: {e}"

    if not isinstance(parsed_args, dict):
        return False, f"Arguments must be a JSON object, got {type(parsed_args).__name__}"

    # Find tool definition
    tool_def: dict[str, Any] | None = None
    for tool in ALL_TOOLS:
        if tool["function"]["name"] == tool_name:  # type: ignore[index]
            tool_def = tool
            break

    if tool_def is None:
        return False, f"Unknown tool: {tool_name}"

    # Check required fields
    schema = tool_def["function"]["parameters"]
    required = schema.get("required", [])
    for field in required:
        if field not in parsed_args:
            return False, f"Missing required field: {field}"

    # Type checks
    properties = schema.get("properties", {})
    for field, value in parsed_args.items():
        if field not in properties:
            continue
        expected_type = properties[field].get("type")
        if expected_type == "string" and not isinstance(value, str):
            return False, f"Field {field} must be string"
        elif expected_type == "integer" and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            # bool is an int subclass — True must not pass an integer check.
            return False, f"Field {field} must be integer"
        elif expected_type == "boolean" and not isinstance(value, bool):
            return False, f"Field {field} must be boolean"
        elif expected_type == "array":
            if not isinstance(value, list):
                return False, f"Field {field} must be array"
            item_type = properties[field].get("items", {}).get("type")
            if item_type == "string" and not all(isinstance(v, str) for v in value):
                return False, f"Field {field} items must be strings"

    # Tool-specific validation
    if tool_name == "contract_add":
        domain_tags = parsed_args.get("domain_tags", [])
        if len(domain_tags) > 2:
            return False, "domain_tags must have at most 2 items"

    # Index-grounded checks (fail-open)
    # TODO T3: call Rust validate via PyO3
    # For now, fail-open (return True)

    return True, None
