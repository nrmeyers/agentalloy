from __future__ import annotations

from typing import TYPE_CHECKING

from ... import constants as cs
from ... import logs as ls
from ..._logging import logger
from ...types_defs import FunctionRegistryTrieProtocol, TreeSitterNodeProtocol
from ..utils import safe_decode_text

if TYPE_CHECKING:
    from ..import_processor import ImportProcessor


class LuaTypeInferenceEngine:
    __slots__ = (
        "import_processor",
        "function_registry",
        "project_name",
    )

    def __init__(
        self,
        import_processor: ImportProcessor,
        function_registry: FunctionRegistryTrieProtocol,
        project_name: str,
    ):
        self.import_processor = import_processor
        self.function_registry = function_registry
        self.project_name = project_name

    def build_local_variable_type_map(
        self,
        caller_node: TreeSitterNodeProtocol,
        module_qn: str,
    ) -> dict[str, str]:
        local_var_types: dict[str, str] = {}
        stack: list[TreeSitterNodeProtocol] = [caller_node]

        while stack:
            current = stack.pop()
            if current.type == cs.TS_LUA_VARIABLE_DECLARATION:
                self._process_variable_declaration(current, module_qn, local_var_types)
            stack.extend(reversed(current.children))

        logger.debug(ls.LUA_VAR_TYPE_MAP_BUILT, count=len(local_var_types))
        return local_var_types

    def _process_variable_declaration(
        self,
        decl_node: TreeSitterNodeProtocol,
        module_qn: str,
        local_var_types: dict[str, str],
    ) -> None:
        assignment = next(
            (c for c in decl_node.children if c.type == cs.TS_LUA_ASSIGNMENT_STATEMENT),
            None,
        )
        if not assignment:
            return

        var_names = self._extract_var_names(assignment)
        # Pair LHS names with RHS values BY POSITION. The expression list holds
        # every RHS value (calls and non-calls alike), so indexing it directly
        # keeps ``local a, b = foo(), 42`` aligned. The old code filtered to
        # calls first, which shifted the pairing whenever a non-call value
        # appeared (``b`` would inherit ``bar()``'s type from a later slot).
        rhs_values = self._extract_rhs_expressions(assignment)

        for i, var_name in enumerate(var_names):
            if i >= len(rhs_values):
                break
            if var_type := self._infer_lua_variable_type_from_value(rhs_values[i], module_qn):
                local_var_types[var_name] = var_type
                logger.debug(ls.LUA_VAR_INFERRED, var_name=var_name, var_type=var_type)

    def _extract_var_names(self, assignment: TreeSitterNodeProtocol) -> list[str]:
        names: list[str] = []
        for child in assignment.children:
            if child.type != cs.TS_LUA_VARIABLE_LIST:
                continue
            for var_node in child.children:
                if var_node.type == cs.TS_LUA_IDENTIFIER:
                    if decoded := safe_decode_text(var_node):
                        names.append(decoded)
        return names

    def _extract_rhs_expressions(
        self,
        assignment: TreeSitterNodeProtocol,
    ) -> list[TreeSitterNodeProtocol]:
        # Every RHS value in source order (named nodes only — this skips the
        # anonymous `````,```` separators). Non-call values yield no inferred
        # type downstream, but they must keep their slot so the LHS/RHS pairing
        # stays aligned.
        values: list[TreeSitterNodeProtocol] = []
        for child in assignment.children:
            if child.type != cs.TS_LUA_EXPRESSION_LIST:
                continue
            values.extend(expr for expr in child.children if expr.is_named)
        return values

    def _infer_lua_variable_type_from_value(
        self,
        value_node: TreeSitterNodeProtocol,
        module_qn: str,
    ) -> str | None:
        if value_node.type == cs.TS_LUA_FUNCTION_CALL:
            for child in value_node.children:
                if child.type == cs.TS_LUA_METHOD_INDEX_EXPRESSION:
                    class_name = None
                    method_name = None

                    for grandchild in child.children:
                        if grandchild.type == cs.TS_LUA_IDENTIFIER:
                            if class_name is None:
                                class_name = safe_decode_text(grandchild)
                            else:
                                method_name = safe_decode_text(grandchild)

                    if class_name and method_name:
                        if class_qn := self._resolve_lua_class_name(class_name, module_qn):
                            logger.debug(
                                ls.LUA_TYPE_INFERENCE_RETURN,
                                class_name=class_name,
                                method_name=method_name,
                                class_qn=class_qn,
                            )
                            return class_qn

        return None

    def _resolve_lua_class_name(self, class_name: str, module_qn: str) -> str | None:
        if module_qn in self.import_processor.import_mapping:
            import_map = self.import_processor.import_mapping[module_qn]
            if class_name in import_map:
                imported_qn = import_map[class_name]
                full_class_qn = f"{imported_qn}{cs.SEPARATOR_DOT}{class_name}"
                return full_class_qn

        local_class_qn = f"{module_qn}{cs.SEPARATOR_DOT}{class_name}"
        if local_class_qn in self.function_registry:
            return local_class_qn

        method_prefix = f"{local_class_qn}{cs.LUA_METHOD_SEPARATOR}"
        return next(
            (
                local_class_qn
                for qn, _ in self.function_registry.find_with_prefix(local_class_qn)
                if qn.startswith(method_prefix)
            ),
            None,
        )
