"""Codebase-index module — code search & call graphs served under ``/code``.

Optional context module toggled by ``CODE_INDEX_ENABLED``. Heavy dependencies
(tree-sitter + grammars) ship behind the ``[code-index]`` extra; nothing in
this package may be imported unless the toggle is on (``app.create_app`` lazy-
imports it inside the enabled branch).
"""
