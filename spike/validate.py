"""Schema validation (subset of JSON Schema) + ground-truth arg checks."""

from __future__ import annotations

from typing import Any


def validate(value: Any, schema: dict, path: str = "$") -> list[str]:
    """Validate ``value`` against the schema subset used by protocol.py:
    type (object/string/integer), properties, required, additionalProperties,
    enum, minLength, minimum, maximum."""
    errs: list[str] = []
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            return [f"{path}: expected object, got {type(value).__name__}"]
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                errs.append(f"{path}: missing required property '{req}'")
        for k, v in value.items():
            if k in props:
                errs.extend(validate(v, props[k], f"{path}.{k}"))
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}: unexpected property '{k}'")
    elif t == "string":
        if not isinstance(value, str):
            errs.append(f"{path}: expected string, got {type(value).__name__}")
        else:
            if "minLength" in schema and len(value) < schema["minLength"]:
                errs.append(f"{path}: shorter than minLength {schema['minLength']}")
            if "enum" in schema and value not in schema["enum"]:
                errs.append(f"{path}: {value!r} not in enum {schema['enum']}")
    elif t == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            errs.append(f"{path}: expected integer, got {type(value).__name__}")
        else:
            if "minimum" in schema and value < schema["minimum"]:
                errs.append(f"{path}: {value} below minimum {schema['minimum']}")
            if "maximum" in schema and value > schema["maximum"]:
                errs.append(f"{path}: {value} above maximum {schema['maximum']}")
    return errs


def check_args(args: dict[str, Any], check: dict[str, dict]) -> list[str]:
    """Evaluate ground-truth expectations on the filled arguments.

    Rule forms:
      {"exact": v}            value must equal v
      {"omit": true}          argument must be absent
      {"omit_or": v}          absent, or equal to v
      {"min_contains": [...]} value must contain ALL tokens (case-insensitive)
      {"any_of": [...]}       value must contain AT LEAST ONE token (case-insensitive)
    """
    errs: list[str] = []
    for argname, rule in check.items():
        val = args.get(argname)
        if rule.get("omit"):
            if val is not None:
                errs.append(f"{argname}: expected absent, got {val!r}")
        elif "omit_or" in rule:
            if val is not None and val != rule["omit_or"]:
                errs.append(f"{argname}: expected absent or {rule['omit_or']!r}, got {val!r}")
        elif "exact" in rule:
            if val != rule["exact"]:
                errs.append(f"{argname}: expected {rule['exact']!r}, got {val!r}")
        elif "min_contains" in rule:
            if not isinstance(val, str) or not all(t in val.lower() for t in rule["min_contains"]):
                errs.append(f"{argname}: {val!r} missing one of {rule['min_contains']}")
        elif "any_of" in rule:
            if not isinstance(val, str) or not any(t in val.lower() for t in rule["any_of"]):
                errs.append(f"{argname}: {val!r} matches none of {rule['any_of']}")
    return errs
