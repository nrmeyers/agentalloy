from __future__ import annotations

from pathlib import Path

from tree_sitter import Node, QueryCursor

from .. import constants as cs
from .. import logs as ls
from .._logging import logger
from ..language_spec import LanguageSpec
from ..services import IngestorProtocol
from ..types_defs import FunctionRegistryTrieProtocol, LanguageQueries
from .call_resolver import CallResolver
from .cpp import utils as cpp_utils
from .import_processor import ImportProcessor
from .rebind_processor import RebindRegistry
from .type_inference import TypeInferenceEngine
from .utils import get_function_captures, is_method_node


class CallProcessor:
    __slots__ = ("ingestor", "repo_path", "project_name", "_resolver")

    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
        function_registry: FunctionRegistryTrieProtocol,
        import_processor: ImportProcessor,
        type_inference: TypeInferenceEngine,
        class_inheritance: dict[str, list[str]],
        rebind_registry: RebindRegistry | None = None,
    ) -> None:
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.project_name = project_name

        self._resolver = CallResolver(
            function_registry=function_registry,
            import_processor=import_processor,
            type_inference=type_inference,
            class_inheritance=class_inheritance,
            # BUC-1611: pass the rebind registry through so the resolver
            # can swap method qnames at call-resolution time.  Defaults
            # to ``None`` to keep callers unaware of the rebind subsystem
            # if they don't need it (tests, legacy code paths).
            rebind_registry=rebind_registry,
        )

    def _get_node_name(self, node: Node, field: str = cs.FIELD_NAME) -> str | None:
        name_node = node.child_by_field_name(field)
        if not name_node:
            return None
        text = name_node.text
        return None if text is None else text.decode(cs.ENCODING_UTF8)

    def process_calls_in_file(
        self,
        file_path: Path,
        root_node: Node,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        relative_path = file_path.relative_to(self.repo_path)
        logger.debug(ls.CALL_PROCESSING_FILE, path=relative_path)

        # BUC-1603: provenance file_path is stored as a repo-relative POSIX
        # path so it is portable across OSes and stable under repo moves.
        # We compute it once here and thread it through every call-site
        # ingestion below.
        call_site_file_path = relative_path.as_posix()

        try:
            module_qn = cs.SEPARATOR_DOT.join(
                [self.project_name] + list(relative_path.with_suffix("").parts)
            )
            if file_path.name in (cs.INIT_PY, cs.MOD_RS):
                module_qn = cs.SEPARATOR_DOT.join(
                    [self.project_name] + list(relative_path.parent.parts)
                )

            self._process_calls_in_functions(
                root_node, module_qn, language, queries, call_site_file_path
            )
            self._process_calls_in_classes(
                root_node, module_qn, language, queries, call_site_file_path
            )
            self._process_module_level_calls(
                root_node, module_qn, language, queries, call_site_file_path
            )

        except Exception as e:
            logger.error(ls.CALL_PROCESSING_FAILED, path=file_path, error=e)

    def _process_calls_in_functions(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        call_site_file_path: str,
    ) -> None:
        result = get_function_captures(root_node, language, queries)
        if not result:
            return

        lang_config, captures = result
        func_nodes = captures.get(cs.CAPTURE_FUNCTION, [])
        for func_node in func_nodes:
            if not isinstance(func_node, Node):
                continue
            if self._is_method(func_node, lang_config):
                continue

            if language == cs.SupportedLanguage.CPP:
                func_name = cpp_utils.extract_function_name(func_node)
            else:
                func_name = self._get_node_name(func_node)
            if not func_name:
                continue
            if func_qn := self._build_nested_qualified_name(
                func_node, module_qn, func_name, lang_config
            ):
                self._ingest_function_calls(
                    func_node,
                    func_qn,
                    cs.NodeLabel.FUNCTION,
                    module_qn,
                    language,
                    queries,
                    call_site_file_path=call_site_file_path,
                )

    def _get_rust_impl_class_name(self, class_node: Node) -> str | None:
        class_name = self._get_node_name(class_node, cs.FIELD_TYPE)
        if class_name:
            return class_name
        return next(
            (
                child.text.decode(cs.ENCODING_UTF8)
                for child in class_node.children
                if child.type == cs.TS_TYPE_IDENTIFIER and child.is_named and child.text
            ),
            None,
        )

    def _get_class_name_for_node(
        self, class_node: Node, language: cs.SupportedLanguage
    ) -> str | None:
        if language == cs.SupportedLanguage.RUST and class_node.type == cs.TS_IMPL_ITEM:
            return self._get_rust_impl_class_name(class_node)
        return self._get_node_name(class_node)

    def _process_methods_in_class(
        self,
        body_node: Node,
        class_qn: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        call_site_file_path: str,
    ) -> None:
        method_query = queries[language][cs.QUERY_FUNCTIONS]
        if not method_query:
            return
        method_cursor = QueryCursor(method_query)
        method_captures = method_cursor.captures(body_node)
        method_nodes = method_captures.get(cs.CAPTURE_FUNCTION, [])
        for method_node in method_nodes:
            if not isinstance(method_node, Node):
                continue
            method_name = self._get_node_name(method_node)
            if not method_name:
                continue
            method_qn = f"{class_qn}{cs.SEPARATOR_DOT}{method_name}"
            self._ingest_function_calls(
                method_node,
                method_qn,
                cs.NodeLabel.METHOD,
                module_qn,
                language,
                queries,
                class_qn,
                call_site_file_path=call_site_file_path,
            )

    def _process_calls_in_classes(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        call_site_file_path: str,
    ) -> None:
        query = queries[language][cs.QUERY_CLASSES]
        if not query:
            return
        cursor = QueryCursor(query)
        captures = cursor.captures(root_node)
        class_nodes = captures.get(cs.CAPTURE_CLASS, [])

        for class_node in class_nodes:
            if not isinstance(class_node, Node):
                continue
            class_name = self._get_class_name_for_node(class_node, language)
            if not class_name:
                continue
            class_qn = f"{module_qn}{cs.SEPARATOR_DOT}{class_name}"
            if body_node := class_node.child_by_field_name(cs.FIELD_BODY):
                self._process_methods_in_class(
                    body_node,
                    class_qn,
                    module_qn,
                    language,
                    queries,
                    call_site_file_path,
                )

    def _process_module_level_calls(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        call_site_file_path: str,
    ) -> None:
        self._ingest_function_calls(
            root_node,
            module_qn,
            cs.NodeLabel.MODULE,
            module_qn,
            language,
            queries,
            call_site_file_path=call_site_file_path,
        )

    def _get_call_target_name(self, call_node: Node) -> str | None:
        if func_child := call_node.child_by_field_name(cs.TS_FIELD_FUNCTION):
            match func_child.type:
                case (
                    cs.TS_IDENTIFIER
                    | cs.TS_ATTRIBUTE
                    | cs.TS_MEMBER_EXPRESSION
                    | cs.CppNodeType.QUALIFIED_IDENTIFIER
                    | cs.TS_SCOPED_IDENTIFIER
                ):
                    if func_child.text is not None:
                        return str(func_child.text.decode(cs.ENCODING_UTF8))
                case cs.TS_CPP_FIELD_EXPRESSION:
                    field_node = func_child.child_by_field_name(cs.FIELD_FIELD)
                    if field_node and field_node.text:
                        return str(field_node.text.decode(cs.ENCODING_UTF8))
                case cs.TS_PARENTHESIZED_EXPRESSION:
                    return self._get_iife_target_name(func_child)

        match call_node.type:
            case (
                cs.TS_CPP_BINARY_EXPRESSION
                | cs.TS_CPP_UNARY_EXPRESSION
                | cs.TS_CPP_UPDATE_EXPRESSION
            ):
                operator_node = call_node.child_by_field_name(cs.FIELD_OPERATOR)
                if operator_node and operator_node.text:
                    operator_text = operator_node.text.decode(cs.ENCODING_UTF8)
                    return cpp_utils.convert_operator_symbol_to_name(operator_text)
            case cs.TS_METHOD_INVOCATION:
                object_node = call_node.child_by_field_name(cs.FIELD_OBJECT)
                name_node = call_node.child_by_field_name(cs.FIELD_NAME)
                if name_node and name_node.text:
                    method_name = str(name_node.text.decode(cs.ENCODING_UTF8))
                    if not object_node or not object_node.text:
                        return method_name
                    object_text = str(object_node.text.decode(cs.ENCODING_UTF8))
                    return f"{object_text}{cs.SEPARATOR_DOT}{method_name}"

        if name_node := call_node.child_by_field_name(cs.FIELD_NAME):
            if name_node.text is not None:
                return str(name_node.text.decode(cs.ENCODING_UTF8))

        return None

    def _get_iife_target_name(self, parenthesized_expr: Node) -> str | None:
        for child in parenthesized_expr.children:
            match child.type:
                case cs.TS_FUNCTION_EXPRESSION:
                    return f"{cs.IIFE_FUNC_PREFIX}{child.start_point[0]}_{child.start_point[1]}"
                case cs.TS_ARROW_FUNCTION:
                    return f"{cs.IIFE_ARROW_PREFIX}{child.start_point[0]}_{child.start_point[1]}"
        return None

    def _ingest_function_calls(
        self,
        caller_node: Node,
        caller_qn: str,
        caller_type: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        class_context: str | None = None,
        *,
        call_site_file_path: str = "",
    ) -> None:
        calls_query = queries[language].get(cs.QUERY_CALLS)
        if not calls_query:
            return

        local_var_types = self._resolver.type_inference.build_local_variable_type_map(
            caller_node, module_qn, language
        )

        cursor = QueryCursor(calls_query)
        captures = cursor.captures(caller_node)
        call_nodes = captures.get(cs.CAPTURE_CALL, [])

        logger.debug(
            ls.CALL_FOUND_NODES,
            count=len(call_nodes),
            language=language,
            caller=caller_qn,
        )

        for call_node in call_nodes:
            if not isinstance(call_node, Node):
                continue

            # (H) tree-sitter finds ALL call nodes including nested; no recursive processing needed

            call_name = self._get_call_target_name(call_node)
            if not call_name:
                continue

            # BUC-1609: switch every resolver entry point to its
            # ``_with_provenance`` sibling so each branch can carry its
            # own resolved_via + confidence tag through to the edge
            # properties.  Falling through to ``continue`` when the tag is
            # ``None`` preserves the legacy dispatch order: java →
            # function-call → builtin → cpp-operator → drop.
            if language == cs.SupportedLanguage.JAVA and call_node.type == cs.TS_METHOD_INVOCATION:
                tagged_callee = self._resolver.resolve_java_method_call_with_provenance(
                    call_node, module_qn, local_var_types
                )
            else:
                tagged_callee = self._resolver.resolve_function_call_with_provenance(
                    call_name, module_qn, local_var_types, class_context
                )
            if tagged_callee is None:
                tagged_callee = self._resolver.resolve_builtin_call_with_provenance(call_name)
            if tagged_callee is None:
                tagged_callee = self._resolver.resolve_cpp_operator_call_with_provenance(
                    call_name, module_qn
                )
            if tagged_callee is None:
                continue

            callee_type = tagged_callee.callee_type
            callee_qn = tagged_callee.callee_qn

            # BUC-1611: after the normal resolver has picked a callee,
            # consult the rebind registry for any module-level monkey-patch
            # targeting this qname.  When a rebind exists, the callee is
            # swapped to the replacement target and ``rebind_resolved_via``
            # is set to ``'rebound'`` (BUC-1609 reserved value), which
            # overrides the resolver's own resolved_via tag below.
            # ``apply_rebind`` is a safe no-op (returns None for the
            # resolved_via slot) when the resolver was constructed without
            # a rebind_registry or when no rebind targets this qname.
            callee_type, callee_qn, rebind_resolved_via = self._resolver.apply_rebind(
                callee_type, callee_qn
            )

            logger.debug(
                ls.CALL_FOUND,
                caller=caller_qn,
                call_name=call_name,
                callee_type=callee_type,
                callee_qn=callee_qn,
            )

            # BUC-1603: tree-sitter ``start_point`` is a ``(row, column)`` tuple
            # of zero-indexed offsets.  We convert ``row`` to a 1-indexed
            # ``line_start`` (matches Function/Method node ``start_line`` and
            # editor conventions); ``col_start`` stays zero-indexed because
            # that is what every downstream LSP consumer expects.  When the
            # node lacks position info (defensive — should not happen with
            # tree-sitter) we fall back to (0, 0) which sorts to the schema
            # DEFAULT and signals "unknown" to the reader.
            start_point = getattr(call_node, "start_point", None)
            line_start = (start_point[0] + 1) if start_point else 0
            col_start = start_point[1] if start_point else 0

            # BUC-1609 + BUC-1611: resolver provenance with rebind override.
            # Rebound calls inherit the resolver's confidence number (the
            # rebind didn't make the call site any less certain about WHICH
            # qname it picked — that signal still comes from the resolver).
            resolved_via = (
                rebind_resolved_via
                if rebind_resolved_via is not None
                else tagged_callee.resolved_via
            )

            self.ingestor.ensure_relationship_batch(
                (caller_type, cs.KEY_QUALIFIED_NAME, caller_qn),
                cs.RelationshipType.CALLS,
                (callee_type, cs.KEY_QUALIFIED_NAME, callee_qn),
                properties={
                    "file_path": call_site_file_path,
                    "line_start": line_start,
                    "col_start": col_start,
                    "resolved_via": resolved_via,
                    "confidence": tagged_callee.confidence,
                },
            )

    def _build_nested_qualified_name(
        self,
        func_node: Node,
        module_qn: str,
        func_name: str,
        lang_config: LanguageSpec,
    ) -> str | None:
        path_parts: list[str] = []
        current = func_node.parent

        if not isinstance(current, Node):
            logger.warning(ls.CALL_UNEXPECTED_PARENT, node=func_node, parent_type=type(current))
            return None

        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.function_node_types:
                if name_node := current.child_by_field_name(cs.FIELD_NAME):
                    text = name_node.text
                    if text is not None:
                        path_parts.append(text.decode(cs.ENCODING_UTF8))
            elif current.type in lang_config.class_node_types:
                return None

            current = current.parent

        path_parts.reverse()
        if path_parts:
            return f"{module_qn}{cs.SEPARATOR_DOT}{cs.SEPARATOR_DOT.join(path_parts)}{cs.SEPARATOR_DOT}{func_name}"
        return f"{module_qn}{cs.SEPARATOR_DOT}{func_name}"

    def _is_method(self, func_node: Node, lang_config: LanguageSpec) -> bool:
        return is_method_node(func_node, lang_config)
