# Replacement for the upstream `from .config import settings` pydantic-settings
# module. The engine must not read env vars or config files; callers construct
# an EngineConfig (or accept these defaults, which mirror upstream config.py).

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """Tunables consumed by the vendored parsing engine."""

    # BoundedASTCache limits (upstream: CACHE_MAX_ENTRIES / CACHE_MAX_MEMORY_MB).
    cache_max_entries: int = 1000
    cache_max_memory_mb: int = 500
    # Evict 1/N of entries when over the memory budget (CACHE_EVICTION_DIVISOR).
    cache_eviction_divisor: int = 10
    # Fallback ratio when sizeof-based accounting fails (CACHE_MEMORY_THRESHOLD_RATIO).
    cache_memory_threshold_ratio: float = 0.8
    # Flush ingestor buffers every N changed files (FILE_FLUSH_INTERVAL).
    file_flush_interval: int = 500


DEFAULT_ENGINE_CONFIG = EngineConfig()
