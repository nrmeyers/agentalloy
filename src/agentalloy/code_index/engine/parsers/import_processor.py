from functools import lru_cache
from pathlib import Path

from tree_sitter import Node

from .. import constants as cs
from .. import logs as ls
from .._logging import logger
from ..language_spec import LanguageSpec
from ..services import IngestorProtocol
from ..types_defs import FunctionRegistryTrieProtocol, LanguageQueries
from .lua import utils as lua_utils
from .rs import utils as rs_utils
from .stdlib_extractor import (
    StdlibCacheStats,
    StdlibExtractor,
    clear_stdlib_cache,
    flush_stdlib_cache,
    get_stdlib_cache_stats,
    load_persistent_cache,
    save_persistent_cache,
)
from .tsconfig_resolver import TsconfigResolver
from .utils import (
    get_query_cursor,
    safe_decode_text,
    safe_decode_with_fallback,
    sort_captures_by_position,
)


class ImportProcessor:
    __slots__ = (
        "repo_path",
        "project_name",
        "ingestor",
        "function_registry",
        "import_mapping",
        "re_export_mapping",
        "stdlib_extractor",
        "_is_local_module_cached",
        "_is_local_java_import_cached",
        "_tsconfig_resolver",
    )

    def __init__(
        self,
        repo_path: Path,
        project_name: str,
        ingestor: IngestorProtocol | None = None,
        function_registry: FunctionRegistryTrieProtocol | None = None,
    ) -> None:
        self.repo_path = repo_path
        self.project_name = project_name
        self.ingestor = ingestor
        self.function_registry = function_registry
        self.import_mapping: dict[str, dict[str, str]] = {}
        # (H) BUC-1610: re-export sites. Per module, maps the locally-exported
        # (H) name to the qualified name of the actual definition (or the next
        # (H) re-export chain link). Populated alongside import_mapping for
        # (H) TS barrels (export {X} from '...') and Python __init__.py
        # (H) modules (from .submod import X, filtered by __all__ when present).
        # (H) Used by CallResolver to follow chains across barrels.
        self.re_export_mapping: dict[str, dict[str, str]] = {}
        self.stdlib_extractor = StdlibExtractor(function_registry, repo_path, project_name)

        @lru_cache(maxsize=4096)
        def _is_local_module_cached(module_name: str) -> bool:
            return (
                (repo_path / module_name).is_dir()
                or (repo_path / f"{module_name}{cs.EXT_PY}").is_file()
                or (repo_path / module_name / cs.INIT_PY).is_file()
            )

        @lru_cache(maxsize=4096)
        def _is_local_java_import_cached(import_path: str) -> bool:
            top_level = import_path.split(cs.SEPARATOR_DOT)[0]
            return (repo_path / top_level).is_dir()

        self._is_local_module_cached = _is_local_module_cached
        self._is_local_java_import_cached = _is_local_java_import_cached
        # Lazy: only constructed when JS/TS code is actually being resolved.
        self._tsconfig_resolver: TsconfigResolver | None = None

        load_persistent_cache()

    def __del__(self) -> None:
        try:
            save_persistent_cache()
        except Exception:
            pass

    @staticmethod
    def flush_stdlib_cache() -> None:
        flush_stdlib_cache()

    @staticmethod
    def clear_stdlib_cache() -> None:
        clear_stdlib_cache()

    @staticmethod
    def get_stdlib_cache_stats() -> StdlibCacheStats:
        return get_stdlib_cache_stats()

    def parse_imports(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        if language not in queries:
            return
        imports_query = queries[language]["imports"]
        if not imports_query:
            return

        lang_config = queries[language]["config"]

        self.import_mapping[module_qn] = {}
        self.re_export_mapping[module_qn] = {}

        try:
            cursor = get_query_cursor(imports_query)
            # See sort_captures_by_position: raw captures() order is unstable.
            # Import order determines both import_mapping insertion order and
            # which import's full_name last-writes an external Module node's
            # `path` property, so it must be pinned to source order.
            captures = sort_captures_by_position(cursor.captures(root_node))

            match language:
                case cs.SupportedLanguage.PYTHON:
                    self._parse_python_imports(captures, module_qn)
                case cs.SupportedLanguage.JS | cs.SupportedLanguage.TS | cs.SupportedLanguage.TSX:
                    self._parse_js_ts_imports(captures, module_qn)
                case cs.SupportedLanguage.JAVA:
                    self._parse_java_imports(captures, module_qn)
                case cs.SupportedLanguage.RUST:
                    self._parse_rust_imports(captures, module_qn)
                case cs.SupportedLanguage.GO:
                    self._parse_go_imports(captures, module_qn)
                case cs.SupportedLanguage.CPP:
                    self._parse_cpp_imports(captures, module_qn)
                case cs.SupportedLanguage.LUA:
                    self._parse_lua_imports(captures, module_qn)
                case _:
                    self._parse_generic_imports(captures, module_qn, lang_config)

            logger.debug(
                ls.IMP_PARSED_COUNT,
                count=len(self.import_mapping[module_qn]),
                module=module_qn,
            )

            # (H) BUC-1610: for Python __init__.py modules we register every
            # (H) `from .x import Y` as a re-export, then filter that set by
            # (H) __all__ when present. Done here (after the per-language
            # (H) parse) because the __all__ assignment is independent of
            # (H) the import statements and may appear in any order.
            if language == cs.SupportedLanguage.PYTHON:
                self._apply_python_all_filter(root_node, module_qn)

            if self.ingestor:
                for full_name in self.import_mapping[module_qn].values():
                    module_path = self._resolve_module_path(full_name, module_qn, language)

                    self.ingestor.ensure_relationship_batch(
                        (
                            cs.NodeLabel.MODULE,
                            cs.KEY_QUALIFIED_NAME,
                            module_qn,
                        ),
                        cs.RelationshipType.IMPORTS,
                        (
                            cs.NodeLabel.MODULE,
                            cs.KEY_QUALIFIED_NAME,
                            module_path,
                        ),
                    )
                    logger.debug(
                        ls.IMP_CREATED_RELATIONSHIP,
                        from_module=module_qn,
                        to_module=module_path,
                        full_name=full_name,
                    )

                # (H) BUC-1610: emit one RE_EXPORTS edge per distinct target
                # (H) module that this module re-exports from, so the graph
                # (H) captures the barrel / package-init pass-through
                # (H) topology directly. Symbol-level chaining at lookup time
                # (H) lives in CallResolver.
                emitted_reexport_targets: set[str] = set()
                for target_qn in self.re_export_mapping[module_qn].values():
                    target_module = self._target_module_for_reexport(target_qn)
                    if not target_module or target_module == module_qn:
                        continue
                    if target_module in emitted_reexport_targets:
                        continue
                    emitted_reexport_targets.add(target_module)
                    self.ingestor.ensure_relationship_batch(
                        (
                            cs.NodeLabel.MODULE,
                            cs.KEY_QUALIFIED_NAME,
                            module_qn,
                        ),
                        cs.RelationshipType.RE_EXPORTS,
                        (
                            cs.NodeLabel.MODULE,
                            cs.KEY_QUALIFIED_NAME,
                            target_module,
                        ),
                    )
                    logger.debug(
                        ls.IMP_REEXPORT_EDGE,
                        from_module=module_qn,
                        to_module=target_module,
                    )

        except Exception as e:
            logger.warning(ls.IMP_PARSE_FAILED, module=module_qn, error=e)

    @staticmethod
    def _target_module_for_reexport(target_qn: str) -> str | None:
        """Parent module qname for a re-exported symbol qname.

        ``target_qn`` is the qualified name of either a symbol (e.g.
        ``project.math_utils.add``) or, for namespace re-exports, the module
        itself. We return the dotted prefix up to the last component; this
        works for both because the re-export consumer's resolution always
        walks "<module>.<symbol>" pairs. Returns ``None`` for atomic names
        with no separator (no parent module to point at).
        """

        if not target_qn or cs.SEPARATOR_DOT not in target_qn:
            return None
        return target_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]

    def _apply_python_all_filter(self, root_node: Node, module_qn: str) -> None:
        """Filter re_export_mapping by ``__all__`` when the module declares it.

        Per Python semantics, when a module sets ``__all__ = [...]`` it
        explicitly constrains the names exposed via ``from module import *``
        (and convention also gates ``from module import X``). We treat the
        listed names as the authoritative re-export set: names imported into
        the module but absent from ``__all__`` stay in ``import_mapping``
        (so calls *within* the module still resolve) but are removed from
        ``re_export_mapping`` (so consumers do not chain through to them).

        Returns silently when no ``__all__`` assignment is found — re-exports
        registered during ``_parse_python_imports`` remain as-is.
        """

        declared = self._extract_python_all_names(root_node)
        if declared is None:
            return

        site = self.re_export_mapping.get(module_qn)
        if not site:
            return

        # Wildcards (`*<module>`) stay regardless — they expose every symbol
        # of the source module, which __all__ filters at lookup time, not at
        # declaration time.
        for name in list(site.keys()):
            if name.startswith("*"):
                continue
            if name not in declared:
                del site[name]
                logger.debug(
                    ls.IMP_REEXPORT_PY_ALL_FILTERED,
                    symbol=name,
                    module=module_qn,
                )

    @staticmethod
    def _extract_python_all_names(root_node: Node) -> set[str] | None:
        """Return the set of names listed in ``__all__`` at module top level.

        Returns ``None`` when no ``__all__`` assignment is present, so the
        caller can distinguish "no filter declared" from "filter declared,
        empty list". Only handles the simple ``__all__ = ["a", "b"]`` /
        tuple form, which is the dominant convention; dynamic mutation
        (``__all__.append(...)``) is intentionally out of scope for v0.
        """

        names: set[str] | None = None
        for child in root_node.children:
            if child.type != cs.TS_EXPRESSION_STATEMENT:
                continue
            for grandchild in child.children:
                if grandchild.type != cs.TS_PY_ASSIGNMENT:
                    continue
                left = grandchild.child_by_field_name(cs.FIELD_LEFT)
                right = grandchild.child_by_field_name(cs.FIELD_RIGHT)
                if not left or not right:
                    continue
                if left.type != cs.TS_PY_IDENTIFIER:
                    continue
                if safe_decode_text(left) != "__all__":
                    continue
                # tree-sitter-python tags tuples as "tuple" and sets as
                # "set"; both are valid __all__ containers in the wild.
                if right.type not in (cs.TS_PY_LIST, "tuple", "set"):
                    continue
                # First __all__ assignment wins.
                if names is None:
                    names = set()
                for element in right.children:
                    if element.type in (cs.TS_STRING, cs.TS_STRING_LITERAL):
                        text = safe_decode_text(element)
                        if text:
                            names.add(text.strip("'\""))
        return names

    def _parse_python_imports(self, captures: dict, module_qn: str) -> None:
        all_imports = captures.get(cs.CAPTURE_IMPORT, []) + captures.get(cs.CAPTURE_IMPORT_FROM, [])
        for import_node in all_imports:
            if import_node.type == cs.TS_PY_IMPORT_STATEMENT:
                self._handle_python_import_statement(import_node, module_qn)
            elif import_node.type == cs.TS_PY_IMPORT_FROM_STATEMENT:
                self._handle_python_import_from_statement(import_node, module_qn)

    def _handle_python_import_statement(self, import_node: Node, module_qn: str) -> None:
        for child in import_node.named_children:
            match child.type:
                case cs.TS_DOTTED_NAME:
                    self._handle_dotted_name_import(child, module_qn)
                case cs.TS_ALIASED_IMPORT:
                    self._handle_aliased_import(child, module_qn)

    def _handle_dotted_name_import(self, child: Node, module_qn: str) -> None:
        module_name = safe_decode_text(child) or ""
        local_name = module_name.split(cs.SEPARATOR_DOT)[0]
        full_name = self._resolve_import_full_name(module_name, local_name)
        self.import_mapping[module_qn][local_name] = full_name
        logger.debug(ls.IMP_IMPORT, local=local_name, full=full_name)

    def _handle_aliased_import(self, child: Node, module_qn: str) -> None:
        module_name_node = child.child_by_field_name(cs.FIELD_NAME)
        alias_node = child.child_by_field_name(cs.FIELD_ALIAS)
        if not module_name_node or not alias_node:
            return

        module_name = safe_decode_text(module_name_node)
        alias = safe_decode_text(alias_node)
        if not module_name or not alias:
            return

        top_level = module_name.split(cs.SEPARATOR_DOT)[0]
        full_name = self._resolve_import_full_name(module_name, top_level)
        self.import_mapping[module_qn][alias] = full_name
        logger.debug(ls.IMP_ALIASED_IMPORT, alias=alias, full=full_name)

    def _resolve_import_full_name(self, module_name: str, top_level: str) -> str:
        if self._is_local_module(top_level):
            return f"{self.project_name}{cs.SEPARATOR_DOT}{module_name}"
        return module_name

    def _is_local_module(self, module_name: str) -> bool:
        return self._is_local_module_cached(module_name)

    def _is_local_java_import(self, import_path: str) -> bool:
        return self._is_local_java_import_cached(import_path)

    def _resolve_java_import_path(self, import_path: str) -> str:
        if self._is_local_java_import(import_path):
            return f"{self.project_name}{cs.SEPARATOR_DOT}{import_path}"
        return import_path

    def _is_local_js_import(self, full_name: str) -> bool:
        return full_name.startswith(self.project_name + cs.SEPARATOR_DOT)

    def _resolve_js_internal_module(self, full_name: str) -> str:
        if full_name.endswith(cs.IMPORT_DEFAULT_SUFFIX):
            return full_name[: -len(cs.IMPORT_DEFAULT_SUFFIX)]

        parts = full_name.split(cs.SEPARATOR_DOT)
        if len(parts) <= 2:
            return full_name

        potential_module = cs.SEPARATOR_DOT.join(parts[:-1])
        relative_path = cs.SEPARATOR_SLASH.join(parts[1:-1])

        for ext in (cs.EXT_JS, cs.EXT_TS, cs.EXT_JSX, cs.EXT_TSX):
            if (self.repo_path / f"{relative_path}{ext}").is_file():
                return potential_module
            index_path = self.repo_path / relative_path / f"{cs.INDEX_INDEX}{ext}"
            if index_path.is_file():
                return potential_module

        return full_name

    def _is_local_rust_import(self, import_path: str) -> bool:
        return import_path.startswith(cs.RUST_CRATE_PREFIX)

    def _ensure_external_module_node(self, module_path: str, full_name: str) -> None:
        if not self.ingestor or not module_path:
            return
        if cs.SEPARATOR_DOUBLE_COLON in module_path:
            name = module_path.rsplit(cs.SEPARATOR_DOUBLE_COLON, 1)[-1]
        else:
            name = module_path.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        self.ingestor.ensure_node_batch(
            cs.NodeLabel.MODULE,
            {
                cs.KEY_NAME: name,
                cs.KEY_QUALIFIED_NAME: module_path,
                cs.KEY_PATH: full_name,
                cs.KEY_IS_EXTERNAL: True,
            },
        )

    def _resolve_rust_import_path(self, import_path: str, module_qn: str) -> str:
        # (H) crate:: is always relative to the crate root, not the current module.
        # (H) We find the src directory in the qualified name to identify the crate root.
        if self._is_local_rust_import(import_path):
            path_without_crate = import_path[len(cs.RUST_CRATE_PREFIX) :]
            module_parts = module_qn.split(cs.SEPARATOR_DOT)
            try:
                src_index = module_parts.index(cs.LANG_SRC_DIR)
                crate_root_qn = cs.SEPARATOR_DOT.join(module_parts[: src_index + 1])
            except ValueError:
                crate_root_qn = self.project_name
            module_part = path_without_crate.split(cs.SEPARATOR_DOUBLE_COLON)[0]
            return f"{crate_root_qn}{cs.SEPARATOR_DOT}{module_part}"

        parts = import_path.split(cs.SEPARATOR_DOUBLE_COLON)
        module_path = cs.SEPARATOR_DOUBLE_COLON.join(parts[:-1]) if len(parts) > 1 else parts[0]

        self._ensure_external_module_node(module_path, import_path)
        return module_path

    def _resolve_module_path(
        self,
        full_name: str,
        module_qn: str,
        language: cs.SupportedLanguage,
    ) -> str:
        project_prefix = self.project_name + cs.SEPARATOR_DOT
        match language:
            # (H) Java MODULE semantics: Internal imports point to file-level MODULE
            # (H) nodes (e.g., project.utils.StringUtils) because Java files are named
            # (H) after their primary class. External imports point to package-level
            # (H) (e.g., java.util) because we lack source code to create file-level
            # (H) nodes. This asymmetry is intentional.
            case cs.SupportedLanguage.JAVA:
                if full_name.startswith(project_prefix):
                    return full_name
            case cs.SupportedLanguage.JS | cs.SupportedLanguage.TS | cs.SupportedLanguage.TSX:
                if self._is_local_js_import(full_name):
                    return self._resolve_js_internal_module(full_name)
            case cs.SupportedLanguage.RUST:
                return self._resolve_rust_import_path(full_name, module_qn)

        module_path = self.stdlib_extractor.extract_module_path(full_name, language)
        if not module_path.startswith(project_prefix):
            self._ensure_external_module_node(module_path, full_name)
        return module_path

    def _handle_python_import_from_statement(self, import_node: Node, module_qn: str) -> None:
        module_name = self._extract_python_from_module_name(import_node, module_qn)
        if not module_name:
            return

        imported_items = self._extract_python_imported_items(import_node)
        is_wildcard = any(child.type == cs.TS_WILDCARD_IMPORT for child in import_node.children)

        if not imported_items and not is_wildcard:
            return

        base_module = self._resolve_python_base_module(module_name)
        self._register_python_from_imports(module_qn, base_module, imported_items, is_wildcard)

    def _extract_python_from_module_name(self, import_node: Node, module_qn: str) -> str | None:
        module_name_node = import_node.child_by_field_name(cs.FIELD_MODULE_NAME)
        if not module_name_node:
            return None

        if module_name_node.type == cs.TS_DOTTED_NAME:
            return safe_decode_text(module_name_node)
        if module_name_node.type == cs.TS_RELATIVE_IMPORT:
            return self._resolve_relative_import(module_name_node, module_qn)
        return None

    def _extract_python_imported_items(self, import_node: Node) -> list[tuple[str, str]]:
        imported_items: list[tuple[str, str]] = []

        for name_node in import_node.children_by_field_name(cs.FIELD_NAME):
            if item := self._extract_single_python_import(name_node):
                imported_items.append(item)

        return imported_items

    def _extract_single_python_import(self, name_node: Node) -> tuple[str, str] | None:
        if name_node.type == cs.TS_DOTTED_NAME:
            if name := safe_decode_text(name_node):
                return (name, name)
        elif name_node.type == cs.TS_ALIASED_IMPORT:
            original_node = name_node.child_by_field_name(cs.FIELD_NAME)
            alias_node = name_node.child_by_field_name(cs.FIELD_ALIAS)
            if original_node and alias_node:
                original = safe_decode_text(original_node)
                alias = safe_decode_text(alias_node)
                if original and alias:
                    return (alias, original)
        return None

    def _resolve_python_base_module(self, module_name: str) -> str:
        if module_name.startswith(self.project_name):
            return module_name
        top_level = module_name.split(cs.SEPARATOR_DOT)[0]
        return self._resolve_import_full_name(module_name, top_level)

    def _register_python_from_imports(
        self,
        module_qn: str,
        base_module: str,
        imported_items: list[tuple[str, str]],
        is_wildcard: bool,
    ) -> None:
        if is_wildcard:
            wildcard_key = f"*{base_module}"
            self.import_mapping[module_qn][wildcard_key] = base_module
            # (H) BUC-1610: a wildcard `from .x import *` re-exports everything
            # (H) from base_module, mirroring TS `export * from`. We register
            # (H) the wildcard sentinel so suffix-matching resolution can find
            # (H) any symbol through this chain link.
            self.re_export_mapping[module_qn][wildcard_key] = base_module
            logger.debug(ls.IMP_WILDCARD_IMPORT, module=base_module)
            return

        for local_name, original_name in imported_items:
            full_name = f"{base_module}{cs.SEPARATOR_DOT}{original_name}"
            self.import_mapping[module_qn][local_name] = full_name
            # (H) BUC-1610: every `from X import Y` makes Y accessible through
            # (H) the current module as a re-export. The __all__ filter (when
            # (H) declared) is applied after parsing completes — see
            # (H) ``_apply_python_all_filter``.
            self.re_export_mapping[module_qn][local_name] = full_name
            logger.debug(ls.IMP_FROM_IMPORT, local=local_name, full=full_name)
            logger.debug(
                ls.IMP_REEXPORT_REGISTERED,
                module=module_qn,
                exported=local_name,
                target=full_name,
            )

    def _resolve_relative_import(self, relative_node: Node, module_qn: str) -> str:
        module_parts = module_qn.split(cs.SEPARATOR_DOT)[1:]

        dots = 0
        module_name = ""

        for child in relative_node.children:
            if child.type == cs.TS_IMPORT_PREFIX:
                if decoded_text := safe_decode_text(child):
                    dots = len(decoded_text)
            elif child.type == cs.TS_DOTTED_NAME:
                if decoded_name := safe_decode_text(child):
                    module_name = decoded_name

        target_parts = module_parts[:-dots] if dots > 0 else module_parts

        if module_name:
            target_parts.extend(module_name.split(cs.SEPARATOR_DOT))

        return cs.SEPARATOR_DOT.join(target_parts)

    def _parse_js_ts_imports(self, captures: dict, module_qn: str) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type == cs.TS_IMPORT_STATEMENT:
                source_module = None
                for child in import_node.children:
                    if child.type == cs.TS_STRING:
                        source_text = safe_decode_with_fallback(child).strip("'\"")
                        source_module = self._resolve_js_module_path(source_text, module_qn)
                        break

                if not source_module:
                    continue

                for child in import_node.children:
                    if child.type == cs.TS_IMPORT_CLAUSE:
                        self._parse_js_import_clause(child, source_module, module_qn)

            elif import_node.type == cs.TS_LEXICAL_DECLARATION:
                self._parse_js_require(import_node, module_qn)

            elif import_node.type == cs.TS_EXPORT_STATEMENT:
                self._parse_js_reexport(import_node, module_qn)

    def _resolve_js_module_path(self, import_path: str, current_module: str) -> str:
        if not import_path.startswith(cs.PATH_CURRENT_DIR):
            # Try tsconfig.json compilerOptions.paths aliases first. Falls back
            # to the existing slash-to-dot transformation (which produces an
            # External(...) node downstream) when no alias matches.
            alias_qn = self._resolve_via_tsconfig(import_path, current_module)
            if alias_qn is not None:
                return alias_qn
            return import_path.replace(cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT)

        current_parts = current_module.split(cs.SEPARATOR_DOT)[:-1]
        import_parts = import_path.split(cs.SEPARATOR_SLASH)

        for part in import_parts:
            if part == cs.PATH_CURRENT_DIR:
                continue
            if part == cs.PATH_PARENT_DIR:
                if current_parts:
                    current_parts.pop()
            elif part:
                current_parts.append(part)

        return cs.SEPARATOR_DOT.join(current_parts)

    def _resolve_via_tsconfig(self, import_path: str, current_module: str) -> str | None:
        """Try to resolve ``import_path`` as a tsconfig path alias.

        Returns a dotted ``project_name.<parts>`` qualified name when an alias
        matched and the target resolved to an on-disk file inside the repo;
        otherwise returns ``None`` so the caller can fall back to the existing
        slash-to-dot transformation.
        """

        source_file = self._module_qn_to_source_path(current_module)
        if source_file is None:
            return None

        if self._tsconfig_resolver is None:
            self._tsconfig_resolver = TsconfigResolver(self.repo_path)

        resolved = self._tsconfig_resolver.resolve_alias(import_path, source_file)
        if resolved is None:
            return None

        try:
            relative = resolved.relative_to(self.repo_path.resolve())
        except ValueError:
            # Alias pointed outside the repo (e.g. node_modules) -- let the
            # fallback path treat it as an external module.
            return None

        # Strip the file's extension to produce a module qname identical to
        # what ``definition_processor`` records for that file.
        parts = list(relative.with_suffix("").parts)
        if not parts:
            return None
        return cs.SEPARATOR_DOT.join([self.project_name, *parts])

    def _module_qn_to_source_path(self, module_qn: str) -> Path | None:
        """Reverse a dotted module qname back into a probable on-disk path.

        ``module_qn`` is ``project_name.<relative parts of file without suffix>``.
        Each TypeScript / JavaScript extension is probed; first hit wins.
        Returns ``None`` when no file is found -- the caller treats that as a
        signal to skip tsconfig resolution.
        """

        parts = module_qn.split(cs.SEPARATOR_DOT)
        if len(parts) < 2 or parts[0] != self.project_name:
            return None
        relative = Path(*parts[1:])
        base = self.repo_path / relative
        for ext in (
            cs.EXT_TS,
            cs.EXT_TSX,
            cs.EXT_JS,
            cs.EXT_JSX,
        ):
            candidate = base.parent / (base.name + ext)
            if candidate.is_file():
                return candidate
        return None

    def _parse_js_import_clause(
        self, clause_node: Node, source_module: str, current_module: str
    ) -> None:
        for child in clause_node.children:
            if child.type == cs.TS_IDENTIFIER:
                imported_name = safe_decode_with_fallback(child)
                self.import_mapping[current_module][imported_name] = (
                    f"{source_module}{cs.IMPORT_DEFAULT_SUFFIX}"
                )
                logger.debug(ls.IMP_JS_DEFAULT, name=imported_name, module=source_module)

            elif child.type == cs.TS_NAMED_IMPORTS:
                for grandchild in child.children:
                    if grandchild.type == cs.TS_IMPORT_SPECIFIER:
                        name_node = grandchild.child_by_field_name(cs.FIELD_NAME)
                        alias_node = grandchild.child_by_field_name(cs.FIELD_ALIAS)
                        if name_node:
                            imported_name = safe_decode_with_fallback(name_node)
                            local_name = (
                                safe_decode_with_fallback(alias_node)
                                if alias_node
                                else imported_name
                            )
                            self.import_mapping[current_module][local_name] = (
                                f"{source_module}{cs.SEPARATOR_DOT}{imported_name}"
                            )
                            logger.debug(
                                ls.IMP_JS_NAMED,
                                local=local_name,
                                module=source_module,
                                name=imported_name,
                            )

            elif child.type == cs.TS_NAMESPACE_IMPORT:
                for grandchild in child.children:
                    if grandchild.type == cs.TS_IDENTIFIER:
                        namespace_name = safe_decode_with_fallback(grandchild)
                        self.import_mapping[current_module][namespace_name] = source_module
                        logger.debug(
                            ls.IMP_JS_NAMESPACE,
                            name=namespace_name,
                            module=source_module,
                        )
                        break

    def _parse_js_require(self, decl_node: Node, current_module: str) -> None:
        for declarator in decl_node.children:
            if declarator.type == cs.TS_VARIABLE_DECLARATOR:
                name_node = declarator.child_by_field_name(cs.FIELD_NAME)
                value_node = declarator.child_by_field_name(cs.FIELD_VALUE)

                if (
                    name_node
                    and value_node
                    and name_node.type == cs.TS_IDENTIFIER
                    and value_node.type == cs.TS_CALL_EXPRESSION
                ):
                    func_node = value_node.child_by_field_name(cs.FIELD_FUNCTION)
                    args_node = value_node.child_by_field_name(cs.FIELD_ARGUMENTS)

                    if (
                        func_node
                        and args_node
                        and func_node.type == cs.TS_IDENTIFIER
                        and safe_decode_text(func_node) == cs.IMPORT_REQUIRE
                    ):
                        for arg in args_node.children:
                            if arg.type == cs.TS_STRING:
                                var_name = safe_decode_with_fallback(name_node)
                                required_module = safe_decode_with_fallback(arg).strip("'\"")

                                resolved_module = self._resolve_js_module_path(
                                    required_module, current_module
                                )
                                self.import_mapping[current_module][var_name] = resolved_module
                                logger.debug(
                                    ls.IMP_JS_REQUIRE,
                                    var=var_name,
                                    module=resolved_module,
                                )
                                break

    def _parse_js_reexport(self, export_node: Node, current_module: str) -> None:
        source_module = None
        for child in export_node.children:
            if child.type == cs.TS_STRING:
                source_text = safe_decode_with_fallback(child).strip("'\"")
                source_module = self._resolve_js_module_path(source_text, current_module)
                break

        if not source_module:
            return

        for child in export_node.children:
            if child.type == cs.TS_ASTERISK:
                wildcard_key = f"*{source_module}"
                self.import_mapping[current_module][wildcard_key] = source_module
                # (H) BUC-1610: a namespace re-export (`export * from './x'`)
                # (H) re-exports every public symbol of source_module under
                # (H) the same name. We mirror the wildcard sentinel here so
                # (H) the resolver can match by suffix.
                self.re_export_mapping[current_module][wildcard_key] = source_module
                logger.debug(ls.IMP_JS_NAMESPACE_REEXPORT, module=source_module)
                logger.debug(
                    ls.IMP_REEXPORT_REGISTERED,
                    module=current_module,
                    exported=wildcard_key,
                    target=source_module,
                )
            elif child.type == cs.TS_EXPORT_CLAUSE:
                for grandchild in child.children:
                    if grandchild.type == cs.TS_EXPORT_SPECIFIER:
                        name_node = grandchild.child_by_field_name(cs.FIELD_NAME)
                        alias_node = grandchild.child_by_field_name(cs.FIELD_ALIAS)
                        if name_node:
                            original_name = safe_decode_with_fallback(name_node)
                            exported_name = (
                                safe_decode_with_fallback(alias_node)
                                if alias_node
                                else original_name
                            )
                            target_qn = f"{source_module}{cs.SEPARATOR_DOT}{original_name}"
                            self.import_mapping[current_module][exported_name] = target_qn
                            # (H) BUC-1610: register this as a re-export so the
                            # (H) consumer of `current_module` can chain through
                            # (H) to `target_qn` rather than dead-ending here.
                            self.re_export_mapping[current_module][exported_name] = target_qn
                            logger.debug(
                                ls.IMP_JS_REEXPORT,
                                exported=exported_name,
                                module=source_module,
                                original=original_name,
                            )
                            logger.debug(
                                ls.IMP_REEXPORT_REGISTERED,
                                module=current_module,
                                exported=exported_name,
                                target=target_qn,
                            )

    def _parse_java_imports(self, captures: dict, module_qn: str) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type == cs.TS_IMPORT_DECLARATION:
                is_static = False
                imported_path = None
                is_wildcard = False

                for child in import_node.children:
                    if child.type == cs.TS_STATIC:
                        is_static = True
                    elif child.type == cs.TS_SCOPED_IDENTIFIER:
                        imported_path = safe_decode_with_fallback(child)
                    elif child.type == cs.TS_ASTERISK:
                        is_wildcard = True

                if not imported_path:
                    continue

                resolved_path = self._resolve_java_import_path(imported_path)

                if is_wildcard:
                    logger.debug(ls.IMP_JAVA_WILDCARD, path=resolved_path)
                    self.import_mapping[module_qn][f"*{resolved_path}"] = resolved_path
                elif parts := resolved_path.split(cs.SEPARATOR_DOT):
                    imported_name = parts[-1]
                    self.import_mapping[module_qn][imported_name] = resolved_path
                    if is_static:
                        logger.debug(
                            ls.IMP_JAVA_STATIC,
                            name=imported_name,
                            path=resolved_path,
                        )
                    else:
                        logger.debug(
                            ls.IMP_JAVA_IMPORT,
                            name=imported_name,
                            path=resolved_path,
                        )

    def _parse_rust_imports(self, captures: dict, module_qn: str) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type == cs.TS_USE_DECLARATION:
                self._parse_rust_use_declaration(import_node, module_qn)

    def _parse_rust_use_declaration(self, use_node: Node, module_qn: str) -> None:
        imports = rs_utils.extract_use_imports(use_node)
        # (H) BUC-1618: `pub use` (any visibility) re-exports the symbol through
        # (H) the current module. Detect the visibility_modifier child whose
        # (H) leading keyword is `pub` — this also covers `pub(crate) use`,
        # (H) `pub(super) use`, etc.; for re-export topology we don't care
        # (H) about the restriction, only that the consumer can reach through.
        is_pub_reexport = self._rust_use_is_pub(use_node)

        for imported_name, full_path in imports.items():
            self.import_mapping[module_qn][imported_name] = full_path
            logger.debug(ls.IMP_RUST, name=imported_name, path=full_path)

            if not is_pub_reexport:
                continue

            # (H) BUC-1618: register the re-export under the dotted,
            # (H) project-qualified target qn the chain walker expects.
            # (H) Wildcard sentinels (key prefixed with `*`, value = target
            # (H) module) are mirrored from the import_mapping form so the
            # (H) BUC-1617 consumer-side wildcard walker engages unchanged.
            if imported_name.startswith(cs.RS_WILDCARD_PREFIX):
                # full_path here is the wildcard base (e.g. "crate::sub").
                target_module = self._resolve_rust_full_path_qn(full_path, module_qn)
                if not target_module or target_module == module_qn:
                    continue
                # Re-keying to *<dotted_module> matches the form Python /
                # TS register so _walk_reexport's wildcard branch lights up.
                wildcard_key = f"{cs.RS_WILDCARD_PREFIX}{target_module}"
                self.re_export_mapping[module_qn][wildcard_key] = target_module
                logger.debug(
                    ls.IMP_RUST_PUB_USE,
                    name=wildcard_key,
                    target=target_module,
                    kind="wildcard",
                )
                logger.debug(
                    ls.IMP_REEXPORT_REGISTERED,
                    module=module_qn,
                    exported=wildcard_key,
                    target=target_module,
                )
                continue

            # Named re-export: convert "crate::sub::Foo" to
            # "<crate_root>.<sub>.Foo".  Skip non-local (extern crate / std)
            # paths — those can't be chained into a project symbol.
            target_qn = self._resolve_rust_full_path_qn(full_path, module_qn)
            if not target_qn:
                continue
            self.re_export_mapping[module_qn][imported_name] = target_qn
            logger.debug(
                ls.IMP_RUST_PUB_USE,
                name=imported_name,
                target=target_qn,
                kind="named",
            )
            logger.debug(
                ls.IMP_REEXPORT_REGISTERED,
                module=module_qn,
                exported=imported_name,
                target=target_qn,
            )

    @staticmethod
    def _rust_use_is_pub(use_node: Node) -> bool:
        """Return True iff this ``use`` carries any ``pub`` visibility.

        The tree-sitter-rust grammar wraps visibility in a
        ``visibility_modifier`` child whose first token is always ``pub``
        (with optional ``(crate)`` / ``(super)`` / ``(in path)``
        restriction).  For re-export topology we treat all of these
        equivalently — what matters is that the symbol can be reached
        through the re-exporter, not the scope of the restriction.
        """
        for child in use_node.children:
            if child.type == cs.TS_RS_VISIBILITY_MODIFIER:
                for grandchild in child.children:
                    if grandchild.type == cs.RS_KEYWORD_PUB:
                        return True
                # Fallback: some grammar versions inline the `pub` token as
                # text rather than as a dedicated child.
                text = safe_decode_text(child) or ""
                if text.startswith(cs.RS_KEYWORD_PUB):
                    return True
        return False

    def _resolve_rust_full_path_qn(self, rust_path: str, module_qn: str) -> str | None:
        """Resolve a ``crate::a::b::Foo`` style path to a dotted project qn.

        Returns ``None`` for non-local paths (``std::``, extern crate
        absolute paths, bare external identifiers) — those can't be
        chained into a project symbol via re-export.

        Unlike :meth:`_resolve_rust_import_path`, which only resolves the
        first module segment, this walks every segment so the chain
        walker can split the final ``module.symbol`` pair correctly.
        """
        if not rust_path:
            return None
        if not self._is_local_rust_import(rust_path):
            return None

        path_without_crate = rust_path[len(cs.RUST_CRATE_PREFIX) :]
        if not path_without_crate:
            return None

        module_parts = module_qn.split(cs.SEPARATOR_DOT)
        try:
            src_index = module_parts.index(cs.LANG_SRC_DIR)
            crate_root_qn = cs.SEPARATOR_DOT.join(module_parts[: src_index + 1])
        except ValueError:
            crate_root_qn = self.project_name

        # Replace `::` with `.` so the chain walker's rsplit(".", 1) lands
        # on (module, symbol).  Filter empties from trailing `::`.
        dotted_parts = [
            part for part in path_without_crate.split(cs.SEPARATOR_DOUBLE_COLON) if part
        ]
        if not dotted_parts:
            return None
        return cs.SEPARATOR_DOT.join([crate_root_qn, *dotted_parts])

    def _parse_go_imports(self, captures: dict, module_qn: str) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type == cs.TS_GO_IMPORT_DECLARATION:
                self._parse_go_import_declaration(import_node, module_qn)

    def _parse_go_import_declaration(self, import_node: Node, module_qn: str) -> None:
        for child in import_node.children:
            if child.type == cs.TS_IMPORT_SPEC:
                self._parse_go_import_spec(child, module_qn)
            elif child.type == cs.TS_IMPORT_SPEC_LIST:
                for grandchild in child.children:
                    if grandchild.type == cs.TS_IMPORT_SPEC:
                        self._parse_go_import_spec(grandchild, module_qn)

    def _parse_go_import_spec(self, spec_node: Node, module_qn: str) -> None:
        alias_name = None
        import_path = None

        for child in spec_node.children:
            if child.type == cs.TS_PACKAGE_IDENTIFIER:
                alias_name = safe_decode_with_fallback(child)
            elif child.type == cs.TS_INTERPRETED_STRING_LITERAL:
                import_path = safe_decode_with_fallback(child).strip('"')

        if import_path:
            package_name = alias_name or import_path.split(cs.SEPARATOR_SLASH)[-1]
            self.import_mapping[module_qn][package_name] = import_path
            logger.debug(ls.IMP_GO, package=package_name, path=import_path)

    def _parse_cpp_imports(self, captures: dict, module_qn: str) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type == cs.TS_PREPROC_INCLUDE:
                self._parse_cpp_include(import_node, module_qn)
            elif import_node.type == cs.TS_TEMPLATE_FUNCTION:
                self._parse_cpp_module_import(import_node, module_qn)
            elif import_node.type == cs.TS_DECLARATION:
                self._parse_cpp_module_declaration(import_node, module_qn)

    def _parse_cpp_include(self, include_node: Node, module_qn: str) -> None:
        include_path = None
        is_system_include = False

        for child in include_node.children:
            if child.type == cs.TS_STRING_LITERAL:
                include_path = safe_decode_with_fallback(child).strip('"')
                is_system_include = False
            elif child.type == cs.TS_SYSTEM_LIB_STRING:
                include_path = safe_decode_with_fallback(child).strip("<>")
                is_system_include = True

        if include_path:
            header_name = include_path.split(cs.SEPARATOR_SLASH)[-1]
            if header_name.endswith(cs.EXT_H) or header_name.endswith(cs.EXT_HPP):
                local_name = header_name.split(cs.SEPARATOR_DOT)[0]
            else:
                local_name = header_name

            if is_system_include:
                full_name = (
                    include_path
                    if include_path.startswith(cs.CPP_STD_PREFIX)
                    else f"{cs.IMPORT_STD_PREFIX}{include_path}"
                )
            else:
                path_parts = (
                    include_path.replace(cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT)
                    .replace(cs.EXT_H, "")
                    .replace(cs.EXT_HPP, "")
                )
                full_name = f"{self.project_name}{cs.SEPARATOR_DOT}{path_parts}"

            self.import_mapping[module_qn][local_name] = full_name
            logger.debug(
                ls.IMP_CPP_INCLUDE,
                local=local_name,
                full=full_name,
                system=is_system_include,
            )

    def _parse_cpp_module_import(self, import_node: Node, module_qn: str) -> None:
        identifier_child = None
        template_args_child = None

        for child in import_node.children:
            if child.type == cs.TS_IDENTIFIER:
                identifier_child = child
            elif child.type == cs.TS_TEMPLATE_ARGUMENT_LIST:
                template_args_child = child

        if (
            identifier_child
            and safe_decode_text(identifier_child) == cs.IMPORT_IMPORT
            and template_args_child
        ):
            module_name = None
            for child in template_args_child.children:
                if child.type == cs.TS_TYPE_DESCRIPTOR:
                    for desc_child in child.children:
                        if desc_child.type == cs.TS_TYPE_IDENTIFIER:
                            module_name = safe_decode_with_fallback(desc_child)
                            break
                elif child.type == cs.TS_TYPE_IDENTIFIER:
                    module_name = safe_decode_with_fallback(child)

            if module_name:
                local_name = module_name
                full_name = f"{cs.IMPORT_STD_PREFIX}{module_name}"

                self.import_mapping[module_qn][local_name] = full_name
                logger.debug(ls.IMP_CPP_MODULE, local=local_name, full=full_name)

    def _parse_cpp_module_declaration(self, decl_node: Node, module_qn: str) -> None:
        decoded_text = safe_decode_text(decl_node)
        if not decoded_text:
            return
        decl_text = decoded_text.strip()

        if decl_text.startswith(cs.CPP_MODULE_PREFIX) and not decl_text.startswith(
            cs.CPP_MODULE_PRIVATE_PREFIX
        ):
            parts = decl_text.split()
            if len(parts) >= 2:
                self._register_cpp_module_mapping(parts, 1, module_qn, ls.IMP_CPP_MODULE_IMPL)
        elif decl_text.startswith(cs.CPP_EXPORT_MODULE_PREFIX):
            parts = decl_text.split()
            if len(parts) >= 3:
                self._register_cpp_module_mapping(parts, 2, module_qn, ls.IMP_CPP_MODULE_IFACE)
        elif cs.CPP_IMPORT_PARTITION_PREFIX in decl_text:
            colon_pos = decl_text.find(cs.SEPARATOR_COLON)
            if colon_pos != -1:
                if partition_part := decl_text[colon_pos + 1 :].split(";")[0].strip():
                    partition_name = f"{cs.CPP_PARTITION_PREFIX}{partition_part}"
                    full_name = f"{self.project_name}{cs.SEPARATOR_DOT}{partition_part}"
                    self.import_mapping[module_qn][partition_name] = full_name
                    logger.debug(
                        ls.IMP_CPP_PARTITION,
                        partition=partition_name,
                        full=full_name,
                    )

    def _register_cpp_module_mapping(
        self, parts: list[str], name_index: int, module_qn: str, log_template: str
    ) -> None:
        module_name = parts[name_index].rstrip(";")
        self.import_mapping[module_qn][module_name] = (
            f"{self.project_name}{cs.SEPARATOR_DOT}{module_name}"
        )
        logger.debug(log_template, name=module_name)

    def _parse_generic_imports(
        self, captures: dict, module_qn: str, lang_config: LanguageSpec
    ) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            logger.debug(
                ls.IMP_GENERIC,
                language=lang_config.language,
                node_type=import_node.type,
            )

    def _parse_lua_imports(self, captures: dict, module_qn: str) -> None:
        for call_node in captures.get(cs.CAPTURE_IMPORT, []):
            if self._lua_is_require_call(call_node):
                if module_path := self._lua_extract_require_arg(call_node):
                    local_name = (
                        self._lua_extract_assignment_lhs(call_node)
                        or module_path.split(cs.SEPARATOR_DOT)[-1]
                    )
                    resolved = self._resolve_lua_module_path(module_path, module_qn)
                    self.import_mapping[module_qn][local_name] = resolved
            elif self._lua_is_pcall_require(call_node):
                if module_path := self._lua_extract_pcall_require_arg(call_node):
                    local_name = (
                        self._lua_extract_pcall_assignment_lhs(call_node)
                        or module_path.split(cs.SEPARATOR_DOT)[-1]
                    )
                    resolved = self._resolve_lua_module_path(module_path, module_qn)
                    self.import_mapping[module_qn][local_name] = resolved

            elif self._lua_is_stdlib_call(call_node):
                if stdlib_module := self._lua_extract_stdlib_module(call_node):
                    self.import_mapping[module_qn][stdlib_module] = stdlib_module

    def _lua_is_require_call(self, call_node: Node) -> bool:
        first_child = call_node.children[0] if call_node.children else None
        if first_child and first_child.type == cs.TS_IDENTIFIER:
            return safe_decode_text(first_child) == cs.IMPORT_REQUIRE
        return False

    def _lua_is_pcall_require(self, call_node: Node) -> bool:
        first_child = call_node.children[0] if call_node.children else None
        if not (
            first_child
            and first_child.type == cs.TS_IDENTIFIER
            and safe_decode_text(first_child) == cs.IMPORT_PCALL
        ):
            return False

        args = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        if not args:
            return False

        first_arg_node = next(
            (child for child in args.children if child.type not in cs.PUNCTUATION_TYPES),
            None,
        )

        return (
            first_arg_node is not None
            and first_arg_node.type == cs.TS_IDENTIFIER
            and safe_decode_text(first_arg_node) == cs.IMPORT_REQUIRE
        )

    def _lua_extract_require_arg(self, call_node: Node) -> str | None:
        args = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        candidates = args.children if args else call_node.children
        for node in candidates:
            if node.type in cs.LUA_STRING_TYPES:
                if decoded := safe_decode_text(node):
                    return decoded.strip("'\"")
        return None

    def _lua_extract_pcall_require_arg(self, call_node: Node) -> str | None:
        args = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        if not args:
            return None
        found_require = False
        for child in args.children:
            if found_require and child.type in cs.LUA_STRING_TYPES:
                if decoded := safe_decode_text(child):
                    return decoded.strip("'\"")
            if child.type == cs.TS_IDENTIFIER and safe_decode_text(child) == cs.IMPORT_REQUIRE:
                found_require = True
        return None

    def _lua_extract_assignment_lhs(self, call_node: Node) -> str | None:
        return lua_utils.extract_assigned_name(call_node, accepted_var_types=(cs.TS_IDENTIFIER,))

    def _lua_extract_pcall_assignment_lhs(self, call_node: Node) -> str | None:
        return lua_utils.extract_pcall_second_identifier(call_node)

    def _resolve_lua_module_path(self, import_path: str, current_module: str) -> str:
        if import_path.startswith(cs.PATH_RELATIVE_PREFIX) or import_path.startswith(
            cs.PATH_PARENT_PREFIX
        ):
            parts = current_module.split(cs.SEPARATOR_DOT)[:-1]
            rel_parts = list(
                import_path.replace("\\", cs.SEPARATOR_SLASH).split(cs.SEPARATOR_SLASH)
            )
            for p in rel_parts:
                if p == cs.PATH_CURRENT_DIR:
                    continue
                if p == cs.PATH_PARENT_DIR:
                    if parts:
                        parts.pop()
                elif p:
                    parts.append(p)
            return cs.SEPARATOR_DOT.join(parts)
        dotted = import_path.replace(cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT)

        try:
            relative_file = dotted.replace(cs.SEPARATOR_DOT, cs.SEPARATOR_SLASH) + cs.EXT_LUA
            if (self.repo_path / relative_file).is_file():
                return f"{self.project_name}{cs.SEPARATOR_DOT}{dotted}"
            if (self.repo_path / f"{dotted}{cs.EXT_LUA}").is_file():
                return f"{self.project_name}{cs.SEPARATOR_DOT}{dotted}"
        except OSError:
            pass

        return dotted

    def _lua_is_stdlib_call(self, call_node: Node) -> bool:
        if not call_node.children:
            return False

        first_child = call_node.children[0]
        if first_child.type == cs.TS_DOT_INDEX_EXPRESSION and (
            first_child.children and first_child.children[0].type == cs.TS_IDENTIFIER
        ):
            module_name = safe_decode_text(first_child.children[0])
            return module_name in cs.LUA_STDLIB_MODULES

        return False

    def _lua_extract_stdlib_module(self, call_node: Node) -> str | None:
        if not call_node.children:
            return None

        first_child = call_node.children[0]
        if first_child.type == cs.TS_DOT_INDEX_EXPRESSION and (
            first_child.children and first_child.children[0].type == cs.TS_IDENTIFIER
        ):
            return safe_decode_text(first_child.children[0])

        return None
