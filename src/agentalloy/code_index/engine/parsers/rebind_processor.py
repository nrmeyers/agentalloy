"""BUC-1611: module-level method rebinding (Python monkey-patching).

Detects the pattern::

    # patcher.py
    from original import Widget
    from elsewhere import custom_render

    Widget.render = custom_render

After this top-level assignment, calls to ``Widget().render()`` should
resolve to ``custom_render`` rather than the original ``Widget.render``.
The vanilla call resolver does not see this rebinding, so every call site
that targets ``Widget.render`` ends up pointing at the original symbol —
producing a graph that disagrees with the language's runtime semantics.

This module walks every Python module's top-level ``assignment`` nodes
looking for the pattern ``<ClassName>.<attribute> = <expression>``,
resolves the LHS to a class qname and the RHS to a function/method qname,
and records the rebinding in a process-wide :class:`RebindRegistry`.

The :class:`~codebase_rag.parsers.call_resolver.CallResolver` consults the
registry on every Python call resolution and, when a hit exists, swaps the
candidate callee qname for the replacement.  CALLS edges that were
rerouted through a rebinding are tagged with ``resolved_via="rebound"``
(BUC-1609 reserves this value — until BUC-1609 lands, the property is
written defensively and silently dropped by ingestors that haven't
declared the column).

Scope: Python-only for v1.  TypeScript/JS class-extension patterns
(``Klass.prototype.method = ...``) are structurally different and tracked
under a separate ticket.

Resolver-order decision
-----------------------
"Latest wins" is defined as **the last assignment seen during indexing**.
Concretely:

  1. Within one module, the registry keeps the assignment with the
     highest ``line_start`` (top-to-bottom file order — what a Python
     interpreter would see).
  2. Across modules, whichever module's rebinding was registered last
     wins.  Module processing order is driven by ``GraphUpdater._process_files``,
     which walks the AST cache in discovery order — i.e. the indexer's
     filesystem-walk order.  This is *not* the Python runtime's import
     order, but it is deterministic for a given repo snapshot and
     matches the "if I open every file and read top-to-bottom, the last
     one I see wins" mental model the user is most likely to want.

Truly accurate cross-module ordering would require modeling each
module's import graph and replaying assignments in topological order —
that is out of scope for v1.  Single-module determinism plus
last-registered-wins covers the common monkey-patching pattern
(library extension code patches a stdlib symbol once at import time).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tree_sitter import Node

from .. import constants as cs
from .._logging import logger
from ..services import IngestorProtocol
from ..types_defs import FunctionRegistryTrieProtocol, NodeType

if TYPE_CHECKING:
    from .import_processor import ImportProcessor


@dataclass(frozen=True, slots=True)
class Rebind:
    """One module-level rebinding of ``<class>.<attribute>``.

    Attributes
    ----------
    class_qn:
        Qualified name of the class whose attribute was rebound.
    attribute:
        Attribute name on the class (e.g. ``render``).
    new_target_qn:
        Qualified name of the function/method the attribute now points at.
    new_target_type:
        :class:`NodeType` of the replacement target (Method | Function).
    module_qn:
        Module that performed the rebinding (the rebinding *site*, not
        the module that owns the class).
    file_path:
        Repo-relative POSIX path of the rebinding site.
    line_start:
        1-indexed line number of the assignment.
    """

    class_qn: str
    attribute: str
    new_target_qn: str
    new_target_type: NodeType
    module_qn: str
    file_path: str
    line_start: int

    @property
    def original_method_qn(self) -> str:
        """The qname that consumers would naively resolve to *without* the rebind.

        Used as the TO endpoint of the REBINDS edge.
        """
        return f"{self.class_qn}{cs.SEPARATOR_DOT}{self.attribute}"


class RebindRegistry:
    """In-memory store of module-level method rebindings.

    The registry is keyed by ``(class_qn, attribute)`` and stores a list
    of :class:`Rebind` records in registration order — the *last* entry
    is considered authoritative (latest wins).  Within a single module
    that ordering is source-line order (the processor inserts in
    top-to-bottom order); across modules it is indexer discovery order.

    The class also tracks the original-method-qname → list-of-rebinds
    mapping for quick lookup by the call resolver, which only knows the
    method qname that would resolve naively.
    """

    __slots__ = ("_by_target",)

    def __init__(self) -> None:
        # Key: ``original_method_qn`` (i.e. ``f"{class_qn}.{attribute}"``).
        # Value: ordered list of Rebind records.  Last entry wins.
        self._by_target: dict[str, list[Rebind]] = {}

    def add(self, rebind: Rebind) -> None:
        """Register a rebinding.

        The processor calls this once per discovered assignment.  Idempotent
        on (class_qn, attribute, module_qn, line_start) — repeated
        registrations of an identical record are coalesced.  We do not
        deduplicate across different lines or modules: those are
        *different* rebindings and "latest wins" semantics rely on seeing
        them all.
        """
        bucket = self._by_target.setdefault(rebind.original_method_qn, [])
        # Idempotent on full identity — a re-indexed file should not
        # accumulate duplicate REBINDS edges.  We key on (module_qn,
        # line_start, attribute) because that uniquely identifies a
        # source location, and on new_target_qn because re-pointing the
        # same attribute to a different function is meaningfully
        # different and should win.
        key = (
            rebind.module_qn,
            rebind.line_start,
            rebind.attribute,
            rebind.new_target_qn,
        )
        for existing in bucket:
            existing_key = (
                existing.module_qn,
                existing.line_start,
                existing.attribute,
                existing.new_target_qn,
            )
            if existing_key == key:
                return
        bucket.append(rebind)

    def latest_for(self, original_method_qn: str) -> Rebind | None:
        """Return the most recently registered rebinding for an original method.

        "Most recent" is defined by the registration order described in
        :class:`RebindRegistry`'s docstring — last in the list wins.
        Returns ``None`` when no rebinding has been registered for this
        method qname.
        """
        bucket = self._by_target.get(original_method_qn)
        if not bucket:
            return None
        return bucket[-1]

    def all_rebinds(self) -> list[Rebind]:
        """Return every registered rebinding (used by the edge-emit pass)."""
        return [r for bucket in self._by_target.values() for r in bucket]

    def __len__(self) -> int:
        return sum(len(bucket) for bucket in self._by_target.values())


class RebindProcessor:
    """Walks Python ASTs to discover module-level method rebindings.

    One instance is created per :class:`~ProcessorFactory` and shared
    across the whole indexing run.  The processor populates the
    :class:`RebindRegistry` during a dedicated pass that runs *after*
    definitions/imports are ingested (so the function_registry and
    import_mapping are fully populated) but *before* call resolution
    (so the resolver sees the rebindings).

    Only Python is supported in v1.  Other languages either don't have
    this pattern (Rust, Go) or express it differently enough to warrant
    separate handling (JS/TS prototype assignment is already covered by
    ``_ingest_prototype_inheritance`` in :class:`DefinitionProcessor`).
    """

    __slots__ = (
        "ingestor",
        "repo_path",
        "project_name",
        "function_registry",
        "import_processor",
        "registry",
    )

    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
        function_registry: FunctionRegistryTrieProtocol,
        import_processor: ImportProcessor,
    ) -> None:
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.project_name = project_name
        self.function_registry = function_registry
        self.import_processor = import_processor
        self.registry = RebindRegistry()

    # ------------------------------------------------------------------
    # AST walking
    # ------------------------------------------------------------------

    def process_file(
        self,
        file_path: Path,
        root_node: Node,
        language: cs.SupportedLanguage,
    ) -> None:
        """Walk top-level ``assignment`` nodes in a Python module.

        No-op for non-Python files.  Each module-scope assignment of the
        form ``<expr>.<attr> = <rhs>`` is examined; only those where the
        LHS object resolves to a Class qname and the RHS resolves to a
        Function/Method qname are recorded as rebindings.
        """
        if language != cs.SupportedLanguage.PYTHON:
            return

        relative_path = file_path.relative_to(self.repo_path)
        module_qn = self._compute_module_qn(file_path, relative_path)
        file_path_str = relative_path.as_posix()

        for assign_node in self._iter_module_level_assignments(root_node):
            self._handle_assignment(assign_node, module_qn, file_path_str)

    def _compute_module_qn(self, file_path: Path, relative_path: Path) -> str:
        """Mirror DefinitionProcessor/CallProcessor's module-qname computation.

        Centralising here keeps the rebind processor's keys aligned with
        whatever convention the rest of the pipeline uses for Python
        modules and ``__init__`` files.
        """
        if file_path.name == cs.INIT_PY:
            return cs.SEPARATOR_DOT.join([self.project_name] + list(relative_path.parent.parts))
        return cs.SEPARATOR_DOT.join(
            [self.project_name] + list(relative_path.with_suffix("").parts)
        )

    def _iter_module_level_assignments(self, root_node: Node) -> list[Node]:
        """Return only assignments whose immediate parent is an
        ``expression_statement`` whose parent is the module itself.

        Tree-sitter Python wraps every top-level statement in an
        ``expression_statement`` directly under the ``module`` root, so
        the parent chain is ``module > expression_statement > assignment``.
        Assignments inside function/class bodies live under a ``block``
        and are intentionally skipped — those are local rebinds with
        scope-limited effect, not module-level monkey-patches.
        """
        if root_node.type != cs.TS_PY_MODULE:
            return []
        results: list[Node] = []
        for stmt in root_node.children:
            if stmt.type != cs.TS_PY_EXPRESSION_STATEMENT:
                continue
            # ``expression_statement`` may wrap several comma-separated
            # expressions in principle; in practice for assignments
            # tree-sitter emits one ``assignment`` child.  Iterate to be
            # defensive.
            for child in stmt.children:
                if child.type == cs.TS_PY_ASSIGNMENT:
                    results.append(child)
        return results

    def _handle_assignment(self, assign_node: Node, module_qn: str, file_path_str: str) -> None:
        """Validate and record one assignment if it matches the rebind pattern.

        Pattern shape::

            attribute (left)  =  identifier-or-attribute (right)
              ├ identifier "Widget"          identifier "custom_render"
              ├ .
              └ identifier "render"

        Rejected shapes:
          * LHS is a bare identifier (not an attribute → not a rebind).
          * LHS attribute's *object* doesn't resolve to a class qname
            (instance assignments like ``widget.foo = bar`` look
            structurally identical but are not rebindings).
          * RHS doesn't resolve to a function/method qname in the
            registry (external functions, lambdas, literals — fall back
            to the original target gracefully by simply not recording a
            rebind).
        """
        left = assign_node.child_by_field_name(cs.TS_FIELD_LEFT)
        right = assign_node.child_by_field_name(cs.TS_FIELD_RIGHT)
        if left is None or right is None:
            return
        if left.type != cs.TS_PY_ATTRIBUTE:
            # ``foo = bar`` and tuple unpacking are not class rebinds.
            return

        class_local_name, attribute = self._split_attribute(left)
        if not class_local_name or not attribute:
            return

        class_qn = self._resolve_to_class_qn(class_local_name, module_qn)
        if class_qn is None:
            # LHS object isn't a class we know about — either it's an
            # instance, an external, or a dynamic expression.  Either
            # way, not a class-level rebinding we can model.
            return

        rhs_qn, rhs_type = self._resolve_rhs(right, module_qn)
        if rhs_qn is None or rhs_type is None:
            # RHS is a literal, lambda, or external function — we can't
            # point CALLS edges at something the graph doesn't contain.
            # Gracefully drop the rebinding (test:
            # ``test_external_rhs_falls_back_gracefully``).
            return

        start_point = getattr(assign_node, "start_point", None)
        line_start = (start_point[0] + 1) if start_point else 0

        rebind = Rebind(
            class_qn=class_qn,
            attribute=attribute,
            new_target_qn=rhs_qn,
            new_target_type=rhs_type,
            module_qn=module_qn,
            file_path=file_path_str,
            line_start=line_start,
        )
        self.registry.add(rebind)
        logger.debug(
            "[BUC-1611] Recorded rebind {original} -> {target} ({type}) at {file}:{line}",
            original=rebind.original_method_qn,
            target=rhs_qn,
            type=rhs_type.value,
            file=file_path_str,
            line=line_start,
        )

    # ------------------------------------------------------------------
    # Sub-node helpers
    # ------------------------------------------------------------------

    def _split_attribute(self, attr_node: Node) -> tuple[str | None, str | None]:
        """Split an ``attribute`` node into ``(object_text, attribute_name)``.

        The object can itself be an attribute (``a.b.c.method = ...``) —
        in that case we return the full dotted ``a.b.c`` so the resolver
        can try import-mapping it.
        """
        # tree-sitter Python's ``attribute`` has fields ``object`` and
        # ``attribute``; reading via field names is more robust than
        # indexing children, which would break if the grammar adds
        # whitespace nodes.
        object_node = attr_node.child_by_field_name(cs.TS_FIELD_OBJECT)
        attribute_node = attr_node.child_by_field_name(cs.TS_FIELD_ATTRIBUTE)
        if object_node is None or attribute_node is None:
            return None, None
        if object_node.text is None or attribute_node.text is None:
            return None, None
        return (
            object_node.text.decode(cs.ENCODING_UTF8),
            attribute_node.text.decode(cs.ENCODING_UTF8),
        )

    def _resolve_to_class_qn(self, local_name: str, module_qn: str) -> str | None:
        """Map a local class name (or dotted path) to a fully-qualified Class.

        Resolution order matches the rest of the pipeline:
          1. Same-module class — ``<module_qn>.<local_name>``
          2. Imported class — the module's import_mapping
          3. Already-fully-qualified — verify against the registry

        Returns ``None`` when the name doesn't resolve to a Class node in
        the function_registry (i.e. it's an instance variable, an
        external object, or a typo).
        """
        # 1. Same-module class definition.
        same_module_qn = f"{module_qn}{cs.SEPARATOR_DOT}{local_name}"
        if self.function_registry.get(same_module_qn) == NodeType.CLASS:
            return same_module_qn

        # 2. Imported class via the module's import map.
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        if local_name in import_map:
            imported_qn = import_map[local_name]
            if self.function_registry.get(imported_qn) == NodeType.CLASS:
                return imported_qn

        # 3. Already a fully-qualified class qname.
        if self.function_registry.get(local_name) == NodeType.CLASS:
            return local_name

        return None

    def _resolve_rhs(self, rhs_node: Node, module_qn: str) -> tuple[str | None, NodeType | None]:
        """Resolve the RHS to a callable qname in the function registry.

        Handles two shapes:
          * Bare identifier: ``custom_render`` — look up in same-module
            then in import_mapping.
          * Attribute: ``other_mod.fn`` or ``Klass.method`` — try
            import-mapping the head, then verify the resulting qname is
            in the registry.

        Anything else (lambda, call expression, literal, conditional)
        returns ``(None, None)``.
        """
        if rhs_node.type == cs.TS_IDENTIFIER:
            return self._resolve_identifier(rhs_node, module_qn)
        if rhs_node.type == cs.TS_PY_ATTRIBUTE:
            return self._resolve_attribute_rhs(rhs_node, module_qn)
        return None, None

    def _resolve_identifier(
        self, ident_node: Node, module_qn: str
    ) -> tuple[str | None, NodeType | None]:
        if ident_node.text is None:
            return None, None
        name = ident_node.text.decode(cs.ENCODING_UTF8)

        # Same-module function.
        same_module = f"{module_qn}{cs.SEPARATOR_DOT}{name}"
        if (node_type := self.function_registry.get(same_module)) is not None:
            if node_type in (NodeType.FUNCTION, NodeType.METHOD):
                return same_module, node_type

        # Imported callable.
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        if name in import_map:
            imported_qn = import_map[name]
            node_type = self.function_registry.get(imported_qn)
            if node_type in (NodeType.FUNCTION, NodeType.METHOD):
                return imported_qn, node_type

        return None, None

    def _resolve_attribute_rhs(
        self, attr_node: Node, module_qn: str
    ) -> tuple[str | None, NodeType | None]:
        head, tail = self._split_attribute(attr_node)
        if head is None or tail is None:
            return None, None

        # Try: head is an imported module/class; full qname = import + tail.
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        if head in import_map:
            candidate = f"{import_map[head]}{cs.SEPARATOR_DOT}{tail}"
            node_type = self.function_registry.get(candidate)
            if node_type in (NodeType.FUNCTION, NodeType.METHOD):
                return candidate, node_type

        # Try: head is a same-module class; candidate is its method.
        same_module_head = f"{module_qn}{cs.SEPARATOR_DOT}{head}"
        if self.function_registry.get(same_module_head) == NodeType.CLASS:
            candidate = f"{same_module_head}{cs.SEPARATOR_DOT}{tail}"
            node_type = self.function_registry.get(candidate)
            if node_type in (NodeType.FUNCTION, NodeType.METHOD):
                return candidate, node_type

        return None, None

    # ------------------------------------------------------------------
    # Edge emission
    # ------------------------------------------------------------------

    def emit_rebind_edges(self) -> None:
        """Emit one REBINDS edge per discovered rebinding.

        Called once after every file's ``process_file`` has run.  Each
        edge goes from the *rebinding module* (where the assignment
        lives) to the *original method* (the qname consumers would
        naively resolve to without the rebind).  The replacement target
        is carried as the ``new_target`` property.
        """
        for rebind in self.registry.all_rebinds():
            # The TO endpoint is the *original* callable that the
            # consumer would have resolved to without the rebind.  By
            # construction it is always a class attribute, so it is a
            # Method node (``<class_qn>.<attribute>``).  The replacement
            # target's NodeType is carried as the ``new_target`` STRING
            # property — see schema table declaration in
            # ``services/ladybug_schema.py``.
            self.ingestor.ensure_relationship_batch(
                (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, rebind.module_qn),
                cs.RelationshipType.REBINDS,
                (
                    cs.NodeLabel.METHOD,
                    cs.KEY_QUALIFIED_NAME,
                    rebind.original_method_qn,
                ),
                properties={
                    "new_target": rebind.new_target_qn,
                    "new_target_type": rebind.new_target_type.value,
                    "file_path": rebind.file_path,
                    "line_start": rebind.line_start,
                },
            )
        if len(self.registry):
            logger.info(
                "[BUC-1611] Emitted {n} REBINDS edges across {m} unique class.attributes",
                n=len(self.registry),
                m=len(self.registry._by_target),  # noqa: SLF001 — intentional internal probe
            )
