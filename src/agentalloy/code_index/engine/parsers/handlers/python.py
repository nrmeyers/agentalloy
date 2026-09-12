from __future__ import annotations

from typing import TYPE_CHECKING

from ... import constants as cs
from ..utils import safe_decode_text
from .base import BaseLanguageHandler

if TYPE_CHECKING:
    from ...types_defs import ASTNode


class PythonHandler(BaseLanguageHandler):
    __slots__ = ()

    def extract_decorators(self, node: ASTNode) -> list[str]:
        if not node.parent or node.parent.type != cs.TS_PY_DECORATED_DEFINITION:
            return []
        return [
            decorator_text
            for child in node.parent.children
            if child.type == cs.TS_PY_DECORATOR
            if (decorator_text := safe_decode_text(child))
        ]

    # (H) BUC-1602: classify functions/methods as async / generator.
    def is_async_function(self, node: ASTNode) -> bool:
        return is_async_function(node)

    def is_generator_function(self, node: ASTNode) -> bool:
        return is_generator_function(node)


def is_async_function(func_node: ASTNode) -> bool:
    """Return True if ``func_node`` is an ``async def``.

    Tree-sitter-python emits the ``async`` keyword as a sibling child of
    ``function_definition`` (before the ``def`` keyword), so we just look
    for it directly among the immediate children — there is no separate
    ``async_function_definition`` node type in this grammar.
    """
    if func_node.type != cs.TS_PY_FUNCTION_DEFINITION:
        return False
    return any(child.type == cs.TS_PY_ASYNC for child in func_node.children)


def is_generator_function(func_node: ASTNode) -> bool:
    """Return True if ``func_node`` is a generator (its body contains ``yield``).

    Walks the function body iteratively and stops at nested
    function / class / lambda boundaries — a ``yield`` inside an inner
    function does not make the outer function a generator.
    """
    if func_node.type != cs.TS_PY_FUNCTION_DEFINITION:
        return False
    body = func_node.child_by_field_name(cs.FIELD_BODY)
    if body is None:
        return False

    boundary = (
        cs.TS_PY_FUNCTION_DEFINITION,
        cs.TS_PY_CLASS_DEFINITION,
        cs.TS_PY_LAMBDA,
    )
    stack = list(body.children)
    while stack:
        current = stack.pop()
        if current.type in boundary:
            # Yields inside a nested function/lambda/class belong to that
            # inner scope, not to ``func_node``.
            continue
        if current.type == cs.TS_PY_YIELD:
            return True
        stack.extend(current.children)
    return False
