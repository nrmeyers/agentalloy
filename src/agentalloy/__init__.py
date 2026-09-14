"""AgentAlloy — in-package execution layer.

Local agent runtime: LFM2.5-2.6B interpreter + LangGraph workflow +
OverGraph code index (tree-sitter symbol graph, hybrid search) + phase
machine with approval gates, wired behind an unauthenticated local
FastAPI service and an OpenAI-compatible proxy.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agentalloy")
except PackageNotFoundError:  # editable dev checkout without an installed dist
    __version__ = "0.0.0+dev"
