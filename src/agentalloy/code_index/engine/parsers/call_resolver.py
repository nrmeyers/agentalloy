from __future__ import annotations

import re
from collections import deque
from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from .. import logs as ls
from .._logging import logger
from ..types_defs import FunctionRegistryTrieProtocol, NodeType
from .import_processor import ImportProcessor
from .py import resolve_class_name
from .rebind_processor import RebindRegistry
from .type_inference import TypeInferenceEngine

_SEPARATOR_PATTERN = re.compile(r"[.:]|::")
_CHAINED_METHOD_PATTERN = re.compile(r"\.([^.()]+)$")


# ---------------------------------------------------------------------------
# BUC-1609: CALLS resolver provenance — taxonomy + confidence
# ---------------------------------------------------------------------------
# Every CALLS edge is annotated with two properties so downstream consumers
# (blast-radius queries, mergeAndRank, code-indexer-service's planned
# ``min_confidence`` filter) can distinguish high-confidence bindings from
# fuzzy guesses.
#
# ``resolved_via`` is a small enum of *how* the resolver landed on the
# callee qname; ``confidence`` is a float in [0.0, 1.0] suitable for
# multiplicative score boosts/penalties.  The constants below are the
# canonical values — code outside this module should compare against them
# rather than typing the strings directly so a future renaming is a single
# search-replace.
#
# Reserved values that no current resolver emits:
#   - ``RESOLVED_VIA_REBOUND`` — BUC-1611 (method rebinding)
#   - ``RESOLVED_VIA_SCIP``    — BUC-1615 (scip-typescript)
# Both are exported so downstream consumers can include them in their
# match arms without redefining the strings; they are guarded by
# ``_EMITTABLE_RESOLVED_VIA`` so the resolver itself never accidentally
# emits them before the sibling tickets land.
RESOLVED_VIA_EXACT = "exact"
RESOLVED_VIA_HEURISTIC = "heuristic"
RESOLVED_VIA_WILDCARD = "wildcard"
RESOLVED_VIA_FALLBACK = "fallback"
RESOLVED_VIA_REBOUND = "rebound"  # reserved — BUC-1611
RESOLVED_VIA_SCIP = "scip"  # reserved — BUC-1615
RESOLVED_VIA_UNKNOWN = "unknown"  # schema DEFAULT for pre-BUC-1609 rows

# Confidence mapping per the ticket.  ``unknown`` is intentionally 1.0 —
# pre-existing edges should not be penalized by a downstream
# ``min_confidence`` filter just because they were ingested before this
# migration ran.
CONFIDENCE_EXACT = 1.0
CONFIDENCE_HEURISTIC = 0.6
CONFIDENCE_WILDCARD = 0.5
CONFIDENCE_FALLBACK = 0.2
CONFIDENCE_UNKNOWN = 1.0

# Values this module is allowed to emit on new edges.  ``rebound`` and
# ``scip`` are reserved for sibling tickets — listing them here as
# *emittable* would let BUC-1609 leak forbidden values, which the guard
# below catches at runtime.
_EMITTABLE_RESOLVED_VIA: frozenset[str] = frozenset(
    {
        RESOLVED_VIA_EXACT,
        RESOLVED_VIA_HEURISTIC,
        RESOLVED_VIA_WILDCARD,
        RESOLVED_VIA_FALLBACK,
        RESOLVED_VIA_UNKNOWN,
    }
)


def _assert_emittable_resolved_via(tag: str) -> None:
    """Defensive guard — BUC-1609 must not emit reserved values."""
    if tag not in _EMITTABLE_RESOLVED_VIA:
        raise ValueError(
            f"resolved_via {tag!r} is reserved for a sibling ticket "
            f"(BUC-1611 'rebound' / BUC-1615 'scip') and must not be "
            f"emitted by the BUC-1609 resolver"
        )


class ResolveResult(NamedTuple):
    """A tagged resolver outcome.

    ``callee_type`` and ``callee_qn`` mirror the legacy ``tuple[str, str]``
    shape, so any caller that destructures the first two fields keeps
    working.  ``resolved_via`` + ``confidence`` carry BUC-1609 provenance.
    """

    callee_type: str
    callee_qn: str
    resolved_via: str
    confidence: float

    @classmethod
    def from_tuple(
        cls,
        result: tuple[str, str] | None,
        resolved_via: str,
        confidence: float,
    ) -> ResolveResult | None:
        """Wrap a legacy ``(type, qn)`` tuple with provenance.

        Returns ``None`` when ``result`` is ``None`` so callers can use the
        usual walrus / short-circuit patterns.
        """
        if result is None:
            return None
        _assert_emittable_resolved_via(resolved_via)
        callee_type, callee_qn = result
        return cls(callee_type, callee_qn, resolved_via, confidence)


class CallResolver:
    __slots__ = (
        "function_registry",
        "import_processor",
        "type_inference",
        "class_inheritance",
        "rebind_registry",
    )

    def __init__(
        self,
        function_registry: FunctionRegistryTrieProtocol,
        import_processor: ImportProcessor,
        type_inference: TypeInferenceEngine,
        class_inheritance: dict[str, list[str]],
        rebind_registry: RebindRegistry | None = None,
    ) -> None:
        self.function_registry = function_registry
        self.import_processor = import_processor
        self.type_inference = type_inference
        self.class_inheritance = class_inheritance
        # BUC-1611: optional — when supplied, ``apply_rebind`` swaps the
        # candidate callee qname for any registered module-level
        # monkey-patch.  ``None`` (default, no-op) preserves the
        # pre-1611 resolution path for callers that don't need it.
        self.rebind_registry = rebind_registry

    def apply_rebind(self, callee_type: str, callee_qn: str) -> tuple[str, str, str | None]:
        """Reroute a resolved callee through any matching module-level rebinding.

        Returns ``(callee_type, callee_qn, resolved_via)`` where
        ``resolved_via`` is:
          * ``"rebound"`` — a REBIND was applied; ``callee_qn`` now
            points at the replacement target.
          * ``None`` — no rebinding registered; the input is returned
            unchanged.

        BUC-1609 reserves the string ``"rebound"`` as a CALLS-edge
        provenance value.  Until BUC-1609 ships, downstream consumers
        that don't yet read the ``resolved_via`` property will silently
        ignore it; the rebind itself still takes effect because the
        callee qname is swapped before the edge is written.
        """
        if self.rebind_registry is None or not callee_qn:
            return callee_type, callee_qn, None

        rebind = self.rebind_registry.latest_for(callee_qn)
        if rebind is None:
            return callee_type, callee_qn, None

        # Translate the registry's NodeType enum back into the string
        # node label that CALLS-edge emission expects.  We only support
        # Method/Function rebinds in v1 (anything else is rejected by
        # the rebind processor's RHS resolver).
        new_label = (
            cs.NodeLabel.METHOD
            if rebind.new_target_type == NodeType.METHOD
            else cs.NodeLabel.FUNCTION
        )
        logger.debug(
            "[BUC-1611] Rebind applied: {orig} -> {new} (resolved_via=rebound)",
            orig=callee_qn,
            new=rebind.new_target_qn,
        )
        return new_label, rebind.new_target_qn, "rebound"

    def _resolve_class_qn_from_type(
        self, var_type: str, import_map: dict[str, str], module_qn: str
    ) -> str:
        if cs.SEPARATOR_DOT in var_type:
            return var_type
        if var_type in import_map:
            return import_map[var_type]
        return self._resolve_class_name(var_type, module_qn) or ""

    def _try_resolve_method(
        self, class_qn: str, method_name: str, separator: str = cs.SEPARATOR_DOT
    ) -> tuple[str, str] | None:
        method_qn = f"{class_qn}{separator}{method_name}"
        if method_qn in self.function_registry:
            return self.function_registry[method_qn], method_qn
        return self._resolve_inherited_method(class_qn, method_name)

    def resolve_function_call(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
        class_context: str | None = None,
    ) -> tuple[str, str] | None:
        if result := self._try_resolve_iife(call_name, module_qn):
            return result

        if self._is_super_call(call_name):
            return self._resolve_super_call(call_name, class_context)

        if cs.SEPARATOR_DOT in call_name and self._is_method_chain(call_name):
            return self._resolve_chained_call(call_name, module_qn, local_var_types)

        if result := self._try_resolve_via_imports(call_name, module_qn, local_var_types):
            return result

        if result := self._try_resolve_same_module(call_name, module_qn):
            return result

        return self._try_resolve_via_trie(call_name, module_qn)

    # ------------------------------------------------------------------
    # BUC-1609: provenance-tagged resolver entry points
    # ------------------------------------------------------------------
    # ``resolve_function_call_with_provenance`` mirrors the dispatch order
    # of ``resolve_function_call`` exactly — every branch returns a
    # ``ResolveResult`` carrying the canonical ``resolved_via`` +
    # ``confidence`` tag for that resolver path.  Keeping the dispatch
    # logic duplicated (rather than wrapping the legacy method) is
    # deliberate: the tag depends on *which branch fired*, which is
    # information that can only be observed at the dispatcher.  Wrapping
    # the legacy method would force a per-branch detection heuristic
    # downstream, which is exactly what we are trying to avoid.
    #
    # Branch → tag mapping (matches the schema-level taxonomy):
    #   _try_resolve_iife            → ("exact",     1.0)
    #   _resolve_super_call          → ("exact",     1.0)
    #   _resolve_chained_call        → ("heuristic", 0.6)
    #   _try_resolve_direct_import   → ("exact",     1.0)
    #   _try_resolve_qualified_call  → ("exact",     1.0)
    #   _try_resolve_wildcard_imports→ ("wildcard",  0.5)
    #   _try_resolve_same_module     → ("exact",     1.0)
    #   _try_resolve_via_trie        → ("heuristic", 0.6)  if single match
    #                                  ("heuristic", 0.6)  if multi-match
    #                                                       (trie sort picks
    #                                                        nearest-import
    #                                                        candidate; still
    #                                                        a fuzzy bind)
    #   resolve_builtin_call         → ("exact",     1.0)
    #   resolve_cpp_operator_call    → ("exact",     1.0)
    #   resolve_java_method_call     → ("exact",     1.0)
    #
    # The ``'fallback'`` tag is reserved for resolver paths that bind to a
    # best-effort External node — the current dispatcher does not have
    # such a path (the trie fallback always lands on a real node), so
    # ``'fallback'`` is unused here.  It exists in the taxonomy so a
    # future "External node best-effort" branch can land it without
    # another schema migration.
    def resolve_function_call_with_provenance(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
        class_context: str | None = None,
    ) -> ResolveResult | None:
        if result := self._try_resolve_iife(call_name, module_qn):
            return ResolveResult.from_tuple(result, RESOLVED_VIA_EXACT, CONFIDENCE_EXACT)

        if self._is_super_call(call_name):
            return ResolveResult.from_tuple(
                self._resolve_super_call(call_name, class_context),
                RESOLVED_VIA_EXACT,
                CONFIDENCE_EXACT,
            )

        if cs.SEPARATOR_DOT in call_name and self._is_method_chain(call_name):
            # Chained calls (``a.b().c()``) infer types one link at a
            # time — each hop is a guess.  Tag as heuristic so downstream
            # filters can deprioritize relative to a direct binding.
            return ResolveResult.from_tuple(
                self._resolve_chained_call(call_name, module_qn, local_var_types),
                RESOLVED_VIA_HEURISTIC,
                CONFIDENCE_HEURISTIC,
            )

        if tagged := self._try_resolve_via_imports_tagged(call_name, module_qn, local_var_types):
            return tagged

        if result := self._try_resolve_same_module(call_name, module_qn):
            return ResolveResult.from_tuple(result, RESOLVED_VIA_EXACT, CONFIDENCE_EXACT)

        # Trie fallback — the resolver couldn't bind via any strict path
        # and is falling back to a registry suffix search, picking the
        # candidate with the smallest import distance.  This is the
        # textbook "name matched within scope but ambiguity existed"
        # case from the ticket spec → heuristic / 0.6.
        return ResolveResult.from_tuple(
            self._try_resolve_via_trie(call_name, module_qn),
            RESOLVED_VIA_HEURISTIC,
            CONFIDENCE_HEURISTIC,
        )

    def _try_resolve_via_imports_tagged(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> ResolveResult | None:
        """Tagged sibling of ``_try_resolve_via_imports``.

        Splits the sub-paths that the untagged variant collapses into one
        return value:
          - direct import (no chain)            → ("exact",    1.0)
          - re-export chain, named hops only    → ("exact",    1.0)
          - re-export chain crossing a wildcard → ("wildcard", 0.5)  (BUC-1617)
          - qualified call                      → ("exact",    1.0)
          - ``import *`` wildcard fallback      → ("wildcard", 0.5)
        """
        if module_qn not in self.import_processor.import_mapping:
            return None

        import_map = self.import_processor.import_mapping[module_qn]

        # BUC-1617: direct imports + re-export chains can legitimately
        # cross a wildcard sentinel (``export * from`` /
        # ``from .x import *``) on the way to a registered symbol.  When
        # they do, downgrade the tag from ``"exact"`` to ``"wildcard"``
        # so downstream confidence filters can de-prioritize the binding
        # relative to a fully-named chain.  Pure-named chains (and direct
        # hits) keep their ``"exact"`` / ``1.0`` provenance.
        if tagged := self._try_resolve_direct_import_with_wildcard_flag(call_name, import_map):
            result, via_wildcard = tagged
            if via_wildcard:
                return ResolveResult.from_tuple(result, RESOLVED_VIA_WILDCARD, CONFIDENCE_WILDCARD)
            return ResolveResult.from_tuple(result, RESOLVED_VIA_EXACT, CONFIDENCE_EXACT)

        if result := self._try_resolve_qualified_call(
            call_name, import_map, module_qn, local_var_types
        ):
            return ResolveResult.from_tuple(result, RESOLVED_VIA_EXACT, CONFIDENCE_EXACT)

        # Wildcard fallback — ``from foo import *`` brings in an
        # unenumerated set of names, so the binding is "could be
        # anything from foo".  Tag with the wildcard taxonomy value.
        return ResolveResult.from_tuple(
            self._try_resolve_wildcard_imports(call_name, import_map),
            RESOLVED_VIA_WILDCARD,
            CONFIDENCE_WILDCARD,
        )

    def resolve_builtin_call_with_provenance(self, call_name: str) -> ResolveResult | None:
        return ResolveResult.from_tuple(
            self.resolve_builtin_call(call_name),
            RESOLVED_VIA_EXACT,
            CONFIDENCE_EXACT,
        )

    def resolve_cpp_operator_call_with_provenance(
        self, call_name: str, module_qn: str
    ) -> ResolveResult | None:
        return ResolveResult.from_tuple(
            self.resolve_cpp_operator_call(call_name, module_qn),
            RESOLVED_VIA_EXACT,
            CONFIDENCE_EXACT,
        )

    def resolve_java_method_call_with_provenance(
        self,
        call_node: Node,
        module_qn: str,
        local_var_types: dict[str, str],
    ) -> ResolveResult | None:
        return ResolveResult.from_tuple(
            self.resolve_java_method_call(call_node, module_qn, local_var_types),
            RESOLVED_VIA_EXACT,
            CONFIDENCE_EXACT,
        )

    def _try_resolve_iife(self, call_name: str, module_qn: str) -> tuple[str, str] | None:
        if not call_name:
            return None
        if not (
            call_name.startswith(cs.IIFE_FUNC_PREFIX) or call_name.startswith(cs.IIFE_ARROW_PREFIX)
        ):
            return None
        iife_qn = f"{module_qn}.{call_name}"
        if iife_qn in self.function_registry:
            return self.function_registry[iife_qn], iife_qn
        return None

    def _is_super_call(self, call_name: str) -> bool:
        return (
            call_name == cs.KEYWORD_SUPER
            or call_name.startswith(f"{cs.KEYWORD_SUPER}.")
            or call_name.startswith(f"{cs.KEYWORD_SUPER}()")
        )

    def _try_resolve_via_imports(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        if module_qn not in self.import_processor.import_mapping:
            return None

        import_map = self.import_processor.import_mapping[module_qn]

        if result := self._try_resolve_direct_import(call_name, import_map):
            return result

        if result := self._try_resolve_qualified_call(
            call_name, import_map, module_qn, local_var_types
        ):
            return result

        return self._try_resolve_wildcard_imports(call_name, import_map)

    def _try_resolve_direct_import(
        self, call_name: str, import_map: dict[str, str]
    ) -> tuple[str, str] | None:
        if call_name not in import_map:
            return None
        imported_qn = import_map[call_name]
        if imported_qn in self.function_registry:
            logger.debug(ls.CALL_DIRECT_IMPORT, call_name=call_name, qn=imported_qn)
            return self.function_registry[imported_qn], imported_qn

        # (H) BUC-1610: the direct lookup landed on a re-export site
        # (e.g. ``barrel.add`` rather than ``math_utils.add``). Follow the
        # re-export chain hop-by-hop until either the qn appears in
        # function_registry, a cycle is detected, or a hop has no further
        # re-export link.
        chained = self._follow_reexport_chain(imported_qn, call_name)
        if chained is not None:
            final_qn, _via_wildcard = chained
            return self.function_registry[final_qn], final_qn
        return None

    def _try_resolve_direct_import_with_wildcard_flag(
        self, call_name: str, import_map: dict[str, str]
    ) -> tuple[tuple[str, str], bool] | None:
        """Tagged sibling of :meth:`_try_resolve_direct_import`.

        Mirrors the untagged dispatch exactly but also surfaces whether the
        winning resolution traversed a wildcard re-export sentinel.  The
        wildcard flag is what lets the provenance dispatcher downgrade the
        emitted ``resolved_via`` tag from ``"exact"`` (named re-export
        chain only) to ``"wildcard"`` (BUC-1617 — at least one hop went
        through ``export * from`` / ``from .x import *``).

        Returns ``(callee, via_wildcard)`` on success, ``None`` on miss.
        """
        if call_name not in import_map:
            return None
        imported_qn = import_map[call_name]
        if imported_qn in self.function_registry:
            logger.debug(ls.CALL_DIRECT_IMPORT, call_name=call_name, qn=imported_qn)
            return (
                (self.function_registry[imported_qn], imported_qn),
                False,
            )

        chained = self._follow_reexport_chain(imported_qn, call_name)
        if chained is not None:
            final_qn, via_wildcard = chained
            return (
                (self.function_registry[final_qn], final_qn),
                via_wildcard,
            )
        return None

    # Max hops we are willing to walk through re-export chains before
    # giving up. Real codebases rarely exceed depth 2-3; cap at 16 to keep
    # pathological / adversarial inputs bounded.
    _REEXPORT_MAX_HOPS = 16

    def _follow_reexport_chain(self, start_qn: str, call_name: str) -> tuple[str, bool] | None:
        """Walk RE_EXPORTS chain until reaching a registered symbol or dead end.

        Each chain hop splits ``current_qn`` into ``(module, symbol)`` and
        looks up that module's ``re_export_mapping[symbol]``.  The visited
        set is keyed on the full qn so any revisit short-circuits cleanly,
        whether the cycle is direct (A->B->A) or longer.

        BUC-1617: when the named lookup fails at a hop, fall back to the
        wildcard sentinels (``*<target_module>``) registered for that
        module.  Each wildcard target is probed recursively for the same
        symbol; the first registered hit wins.  A wildcard hop downgrades
        the chain's overall confidence — the second element of the return
        tuple is ``True`` whenever any hop went through a wildcard
        sentinel.  Cycle detection and the 16-hop ceiling are shared with
        the named chain via a single visited-set and depth counter, so
        cyclic wildcards (A export * from B, B export * from A) cannot
        infinite-loop.

        Returns ``(final_qn, via_wildcard)`` on success, ``None`` on dead
        end / cycle / depth-budget exhaustion.
        """

        re_export_mapping = self.import_processor.re_export_mapping
        visited: set[str] = {start_qn}
        return self._walk_reexport(
            start_qn,
            call_name,
            re_export_mapping,
            visited,
            hops_remaining=self._REEXPORT_MAX_HOPS,
            via_wildcard=False,
        )

    def _walk_reexport(
        self,
        current_qn: str,
        call_name: str,
        re_export_mapping: dict[str, dict[str, str]],
        visited: set[str],
        hops_remaining: int,
        via_wildcard: bool,
    ) -> tuple[str, bool] | None:
        """Recursive helper for :meth:`_follow_reexport_chain`.

        Implemented as a depth-bounded walk that probes the named symbol
        at each hop first, then fans out across wildcard sentinels
        registered for the same module.  The recursive call shares the
        single ``visited`` set and ``hops_remaining`` counter across all
        branches so the global 16-hop ceiling holds even when wildcard
        fan-out widens the search.
        """
        if hops_remaining <= 0:
            logger.debug(
                ls.CALL_REEXPORT_CYCLE,
                call_name=call_name,
                module=current_qn,
            )
            return None
        if cs.SEPARATOR_DOT not in current_qn:
            return None

        module_qn, symbol = current_qn.rsplit(cs.SEPARATOR_DOT, 1)
        site = re_export_mapping.get(module_qn)

        if site is not None and symbol in site:
            next_qn = site[symbol]
            if next_qn not in visited:
                visited.add(next_qn)
                if next_qn in self.function_registry:
                    logger.debug(
                        ls.CALL_REEXPORT_RESOLVED,
                        call_name=call_name,
                        hops=self._REEXPORT_MAX_HOPS - hops_remaining + 1,
                        final_qn=next_qn,
                    )
                    return next_qn, via_wildcard
                if result := self._walk_reexport(
                    next_qn,
                    call_name,
                    re_export_mapping,
                    visited,
                    hops_remaining - 1,
                    via_wildcard,
                ):
                    return result
            else:
                logger.debug(
                    ls.CALL_REEXPORT_CYCLE,
                    call_name=call_name,
                    module=module_qn,
                )

        # BUC-1617: try wildcard sentinels (``*<target_module>``).  Each
        # wildcard sentinel exposes every public name of its target, so
        # re-probe the *same* symbol under each candidate module.  The
        # shared visited-set + hops_remaining is what keeps the cycle and
        # depth guarantees from BUC-1610 intact across the fan-out.
        if site is None:
            return None
        for key, wildcard_target_module in site.items():
            if not key.startswith("*"):
                continue
            candidate_qn = f"{wildcard_target_module}{cs.SEPARATOR_DOT}{symbol}"
            if candidate_qn in visited:
                logger.debug(
                    ls.CALL_REEXPORT_CYCLE,
                    call_name=call_name,
                    module=wildcard_target_module,
                )
                continue
            visited.add(candidate_qn)
            if candidate_qn in self.function_registry:
                logger.debug(
                    ls.CALL_REEXPORT_RESOLVED,
                    call_name=call_name,
                    hops=self._REEXPORT_MAX_HOPS - hops_remaining + 1,
                    final_qn=candidate_qn,
                )
                return candidate_qn, True
            if result := self._walk_reexport(
                candidate_qn,
                call_name,
                re_export_mapping,
                visited,
                hops_remaining - 1,
                via_wildcard=True,
            ):
                return result
        return None

    def _try_resolve_qualified_call(
        self,
        call_name: str,
        import_map: dict[str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        if not self._has_separator(call_name):
            return None

        separator = self._get_separator(call_name)
        parts = call_name.split(separator)

        if len(parts) == 2:
            if result := self._resolve_two_part_call(
                parts, call_name, separator, import_map, module_qn, local_var_types
            ):
                return result

        if len(parts) >= 3 and parts[0] == cs.KEYWORD_SELF:
            return self._resolve_self_attribute_call(
                parts, call_name, import_map, module_qn, local_var_types
            )

        return self._resolve_multi_part_call(
            parts, call_name, import_map, module_qn, local_var_types
        )

    def _has_separator(self, call_name: str) -> bool:
        return (
            cs.SEPARATOR_DOT in call_name
            or cs.SEPARATOR_DOUBLE_COLON in call_name
            or cs.SEPARATOR_COLON in call_name
        )

    def _get_separator(self, call_name: str) -> str:
        if cs.SEPARATOR_DOUBLE_COLON in call_name:
            return cs.SEPARATOR_DOUBLE_COLON
        if cs.SEPARATOR_COLON in call_name:
            return cs.SEPARATOR_COLON
        return cs.SEPARATOR_DOT

    def _try_resolve_wildcard_imports(
        self, call_name: str, import_map: dict[str, str]
    ) -> tuple[str, str] | None:
        for local_name, imported_qn in import_map.items():
            if not local_name.startswith("*"):
                continue
            if result := self._try_wildcard_qns(call_name, imported_qn):
                return result
        return None

    def _try_wildcard_qns(self, call_name: str, imported_qn: str) -> tuple[str, str] | None:
        potential_qns = []
        if cs.SEPARATOR_DOUBLE_COLON not in imported_qn:
            potential_qns.append(f"{imported_qn}.{call_name}")
        potential_qns.append(f"{imported_qn}{cs.SEPARATOR_DOUBLE_COLON}{call_name}")

        for wildcard_qn in potential_qns:
            if wildcard_qn in self.function_registry:
                logger.debug(ls.CALL_WILDCARD, call_name=call_name, qn=wildcard_qn)
                return self.function_registry[wildcard_qn], wildcard_qn
        return None

    def _try_resolve_same_module(self, call_name: str, module_qn: str) -> tuple[str, str] | None:
        same_module_func_qn = f"{module_qn}.{call_name}"
        if same_module_func_qn in self.function_registry:
            logger.debug(ls.CALL_SAME_MODULE, call_name=call_name, qn=same_module_func_qn)
            return self.function_registry[same_module_func_qn], same_module_func_qn
        return None

    def _try_resolve_via_trie(self, call_name: str, module_qn: str) -> tuple[str, str] | None:
        search_name = _SEPARATOR_PATTERN.split(call_name)[-1]
        possible_matches = self.function_registry.find_ending_with(search_name)
        if not possible_matches:
            logger.debug(ls.CALL_UNRESOLVED, call_name=call_name)
            return None

        possible_matches.sort(key=lambda qn: self._calculate_import_distance(qn, module_qn))
        best_candidate_qn = possible_matches[0]
        logger.debug(ls.CALL_TRIE_FALLBACK, call_name=call_name, qn=best_candidate_qn)
        return self.function_registry[best_candidate_qn], best_candidate_qn

    def _resolve_two_part_call(
        self,
        parts: list[str],
        call_name: str,
        separator: str,
        import_map: dict[str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        object_name, method_name = parts

        if result := self._try_resolve_via_local_type(
            object_name,
            method_name,
            separator,
            call_name,
            import_map,
            module_qn,
            local_var_types,
        ):
            return result

        if result := self._try_resolve_via_import(
            object_name, method_name, separator, call_name, import_map
        ):
            return result

        return self._try_resolve_module_method(method_name, call_name, module_qn)

    def _try_resolve_via_local_type(
        self,
        object_name: str,
        method_name: str,
        separator: str,
        call_name: str,
        import_map: dict[str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        if not local_var_types or object_name not in local_var_types:
            return None

        var_type = local_var_types[object_name]

        if class_qn := self._resolve_class_qn_from_type(var_type, import_map, module_qn):
            if result := self._try_method_on_class(
                class_qn, method_name, separator, call_name, object_name, var_type
            ):
                return result

        if var_type in cs.JS_BUILTIN_TYPES:
            return (
                cs.NodeLabel.FUNCTION,
                f"{cs.BUILTIN_PREFIX}{cs.SEPARATOR_DOT}{var_type}{cs.SEPARATOR_PROTOTYPE}{method_name}",
            )
        return None

    def _try_method_on_class(
        self,
        class_qn: str,
        method_name: str,
        separator: str,
        call_name: str,
        object_name: str,
        var_type: str,
    ) -> tuple[str, str] | None:
        method_qn = f"{class_qn}{separator}{method_name}"
        if method_qn in self.function_registry:
            logger.debug(
                ls.CALL_TYPE_INFERRED,
                call_name=call_name,
                method_qn=method_qn,
                obj=object_name,
                var_type=var_type,
            )
            return self.function_registry[method_qn], method_qn

        if inherited := self._resolve_inherited_method(class_qn, method_name):
            logger.debug(
                ls.CALL_TYPE_INFERRED_INHERITED,
                call_name=call_name,
                method_qn=inherited[1],
                obj=object_name,
                var_type=var_type,
            )
            return inherited
        return None

    def _try_resolve_via_import(
        self,
        object_name: str,
        method_name: str,
        separator: str,
        call_name: str,
        import_map: dict[str, str],
    ) -> tuple[str, str] | None:
        if object_name not in import_map:
            return None

        class_qn = self._resolve_imported_class_qn(
            import_map[object_name], object_name, method_name, separator
        )

        registry_separator = separator if separator == cs.SEPARATOR_COLON else cs.SEPARATOR_DOT
        method_qn = f"{class_qn}{registry_separator}{method_name}"

        if method_qn in self.function_registry:
            logger.debug(ls.CALL_IMPORT_STATIC, call_name=call_name, method_qn=method_qn)
            return self.function_registry[method_qn], method_qn
        return None

    def _resolve_imported_class_qn(
        self,
        class_qn: str,
        object_name: str,
        method_name: str,
        separator: str,
    ) -> str:
        if cs.SEPARATOR_DOUBLE_COLON in class_qn:
            class_qn = self._resolve_rust_class_qn(class_qn)

        potential_class_qn = f"{class_qn}.{object_name}"
        test_method_qn = f"{potential_class_qn}{separator}{method_name}"
        if test_method_qn in self.function_registry:
            return potential_class_qn
        return class_qn

    def _resolve_rust_class_qn(self, class_qn: str) -> str:
        rust_parts = class_qn.split(cs.SEPARATOR_DOUBLE_COLON)
        class_name = rust_parts[-1]

        matching_qns = self.function_registry.find_ending_with(class_name)
        return next(
            (qn for qn in matching_qns if self.function_registry.get(qn) == NodeType.CLASS),
            class_qn,
        )

    def _try_resolve_module_method(
        self, method_name: str, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        method_qn = f"{module_qn}.{method_name}"
        if method_qn in self.function_registry:
            logger.debug(ls.CALL_OBJECT_METHOD, call_name=call_name, method_qn=method_qn)
            return self.function_registry[method_qn], method_qn
        return None

    def _resolve_self_attribute_call(
        self,
        parts: list[str],
        call_name: str,
        import_map: dict[str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        attribute_ref = cs.SEPARATOR_DOT.join(parts[:-1])
        method_name = parts[-1]

        if local_var_types and attribute_ref in local_var_types:
            var_type = local_var_types[attribute_ref]
            if class_qn := self._resolve_class_qn_from_type(var_type, import_map, module_qn):
                method_qn = f"{class_qn}.{method_name}"
                if method_qn in self.function_registry:
                    logger.debug(
                        ls.CALL_INSTANCE_ATTR,
                        call_name=call_name,
                        method_qn=method_qn,
                        attr_ref=attribute_ref,
                        var_type=var_type,
                    )
                    return self.function_registry[method_qn], method_qn

                if inherited_method := self._resolve_inherited_method(class_qn, method_name):
                    logger.debug(
                        ls.CALL_INSTANCE_ATTR_INHERITED,
                        call_name=call_name,
                        method_qn=inherited_method[1],
                        attr_ref=attribute_ref,
                        var_type=var_type,
                    )
                    return inherited_method

        return None

    def _resolve_multi_part_call(
        self,
        parts: list[str],
        call_name: str,
        import_map: dict[str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        class_name = parts[0]
        method_name = cs.SEPARATOR_DOT.join(parts[1:])

        if class_name in import_map:
            class_qn = import_map[class_name]
            method_qn = f"{class_qn}.{method_name}"
            if method_qn in self.function_registry:
                logger.debug(
                    ls.CALL_IMPORT_QUALIFIED,
                    call_name=call_name,
                    method_qn=method_qn,
                )
                return self.function_registry[method_qn], method_qn

        if local_var_types and class_name in local_var_types:
            var_type = local_var_types[class_name]
            if class_qn := self._resolve_class_qn_from_type(var_type, import_map, module_qn):
                method_qn = f"{class_qn}.{method_name}"
                if method_qn in self.function_registry:
                    logger.debug(
                        ls.CALL_INSTANCE_QUALIFIED,
                        call_name=call_name,
                        method_qn=method_qn,
                        class_name=class_name,
                        var_type=var_type,
                    )
                    return self.function_registry[method_qn], method_qn

                if inherited_method := self._resolve_inherited_method(class_qn, method_name):
                    logger.debug(
                        ls.CALL_INSTANCE_INHERITED,
                        call_name=call_name,
                        method_qn=inherited_method[1],
                        class_name=class_name,
                        var_type=var_type,
                    )
                    return inherited_method

        return None

    def resolve_builtin_call(self, call_name: str) -> tuple[str, str] | None:
        if call_name in cs.JS_BUILTIN_PATTERNS:
            return (cs.NodeLabel.FUNCTION, f"{cs.BUILTIN_PREFIX}.{call_name}")

        for suffix, method in cs.JS_FUNCTION_PROTOTYPE_SUFFIXES.items():
            if call_name.endswith(suffix):
                return (
                    cs.NodeLabel.FUNCTION,
                    f"{cs.BUILTIN_PREFIX}{cs.SEPARATOR_DOT}Function{cs.SEPARATOR_PROTOTYPE}{method}",
                )

        if cs.SEPARATOR_PROTOTYPE in call_name and (
            call_name.endswith(cs.JS_SUFFIX_CALL) or call_name.endswith(cs.JS_SUFFIX_APPLY)
        ):
            base_call = call_name.rsplit(cs.SEPARATOR_DOT, 1)[0]
            return (cs.NodeLabel.FUNCTION, base_call)

        return None

    def resolve_cpp_operator_call(self, call_name: str, module_qn: str) -> tuple[str, str] | None:
        if not call_name.startswith(cs.OPERATOR_PREFIX):
            return None

        if call_name in cs.CPP_OPERATORS:
            return (cs.NodeLabel.FUNCTION, cs.CPP_OPERATORS[call_name])

        if possible_matches := self.function_registry.find_ending_with(call_name):
            same_module_ops = [
                qn for qn in possible_matches if qn.startswith(module_qn) and call_name in qn
            ]
            candidates = same_module_ops or possible_matches
            candidates.sort(key=lambda qn: (len(qn), qn))
            best = candidates[0]
            return (self.function_registry[best], best)

        return None

    def _is_method_chain(self, call_name: str) -> bool:
        if cs.CHAR_PAREN_OPEN not in call_name or cs.CHAR_PAREN_CLOSE not in call_name:
            return False
        parts = call_name.split(cs.SEPARATOR_DOT)
        method_calls = sum(
            cs.CHAR_PAREN_OPEN in part and cs.CHAR_PAREN_CLOSE in part for part in parts
        )
        return method_calls >= 1 and len(parts) >= 2

    def _resolve_chained_call(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> tuple[str, str] | None:
        match = _CHAINED_METHOD_PATTERN.search(call_name)
        if not match:
            return None

        final_method = match[1]

        object_expr = call_name[: match.start()]

        if object_type := self.type_inference.python_type_inference._infer_expression_return_type(
            object_expr, module_qn, local_var_types
        ):
            full_object_type = object_type
            if cs.SEPARATOR_DOT not in object_type:
                if resolved_class := self._resolve_class_name(object_type, module_qn):
                    full_object_type = resolved_class

            method_qn = f"{full_object_type}.{final_method}"

            if method_qn in self.function_registry:
                logger.debug(
                    ls.CALL_CHAINED,
                    call_name=call_name,
                    method_qn=method_qn,
                    obj_expr=object_expr,
                    obj_type=object_type,
                )
                return self.function_registry[method_qn], method_qn

            if inherited_method := self._resolve_inherited_method(full_object_type, final_method):
                logger.debug(
                    ls.CALL_CHAINED_INHERITED,
                    call_name=call_name,
                    method_qn=inherited_method[1],
                    obj_expr=object_expr,
                    obj_type=object_type,
                )
                return inherited_method

        return None

    def _resolve_super_call(
        self, call_name: str, class_context: str | None = None
    ) -> tuple[str, str] | None:
        match call_name:
            case _ if call_name == cs.KEYWORD_SUPER:
                method_name = cs.KEYWORD_CONSTRUCTOR
            case _ if cs.SEPARATOR_DOT in call_name:
                method_name = call_name.split(cs.SEPARATOR_DOT, 1)[1]
            case _:
                return None

        current_class_qn = class_context
        if not current_class_qn:
            logger.debug(ls.CALL_SUPER_NO_CONTEXT, call_name=call_name)
            return None

        if current_class_qn not in self.class_inheritance:
            logger.debug(ls.CALL_SUPER_NO_INHERITANCE, class_qn=current_class_qn)
            return None

        parent_classes = self.class_inheritance[current_class_qn]
        if not parent_classes:
            logger.debug(ls.CALL_SUPER_NO_PARENTS, class_qn=current_class_qn)
            return None

        if result := self._resolve_inherited_method(current_class_qn, method_name):
            callee_type, parent_method_qn = result
            logger.debug(
                ls.CALL_SUPER_RESOLVED,
                call_name=call_name,
                method_qn=parent_method_qn,
            )
            return callee_type, parent_method_qn

        logger.debug(
            ls.CALL_SUPER_UNRESOLVED,
            call_name=call_name,
            class_qn=current_class_qn,
        )
        return None

    def _resolve_inherited_method(self, class_qn: str, method_name: str) -> tuple[str, str] | None:
        if class_qn not in self.class_inheritance:
            return None

        bfs_queue = deque(self.class_inheritance.get(class_qn, []))
        visited = set(bfs_queue)

        while bfs_queue:
            parent_class_qn = bfs_queue.popleft()
            parent_method_qn = f"{parent_class_qn}.{method_name}"

            if parent_method_qn in self.function_registry:
                return (
                    self.function_registry[parent_method_qn],
                    parent_method_qn,
                )

            if parent_class_qn in self.class_inheritance:
                for grandparent_qn in self.class_inheritance[parent_class_qn]:
                    if grandparent_qn not in visited:
                        visited.add(grandparent_qn)
                        bfs_queue.append(grandparent_qn)

        return None

    def _calculate_import_distance(self, candidate_qn: str, caller_module_qn: str) -> int:
        caller_parts = caller_module_qn.split(cs.SEPARATOR_DOT)
        candidate_parts = candidate_qn.split(cs.SEPARATOR_DOT)

        common_prefix = 0
        for i in range(min(len(caller_parts), len(candidate_parts))):
            if caller_parts[i] == candidate_parts[i]:
                common_prefix += 1
            else:
                break

        base_distance = max(len(caller_parts), len(candidate_parts)) - common_prefix

        if candidate_qn.startswith(cs.SEPARATOR_DOT.join(caller_parts[:-1]) + cs.SEPARATOR_DOT):
            base_distance -= 1

        return base_distance

    def _resolve_class_name(self, class_name: str, module_qn: str) -> str | None:
        return resolve_class_name(
            class_name, module_qn, self.import_processor, self.function_registry
        )

    def resolve_java_method_call(
        self,
        call_node: Node,
        module_qn: str,
        local_var_types: dict[str, str],
    ) -> tuple[str, str] | None:
        java_engine = self.type_inference.java_type_inference

        result = java_engine.resolve_java_method_call(call_node, local_var_types, module_qn)

        if result:
            call_text = (
                call_node.text.decode(cs.ENCODING_UTF8) if call_node.text else cs.TEXT_UNKNOWN
            )
            logger.debug(ls.CALL_JAVA_RESOLVED, call_text=call_text, method_qn=result[1])

        return result
