# Loguru-compatibility shim backed by stdlib logging.
#
# The vendored engine was written against loguru's call forms:
#   logger.info("Found {count} functions", count=n)     # brace kwargs
#   logger.info("Pass 3 parallel: {n} files", n=x)      # brace positional/kw
#   logger.warning("could not remove %s: %s", qn, exc)  # %-style
#   logger.debug("Skipping", name=qn, chars=c)          # bare kwargs (extra)
#
# This module provides a single `logger` object that accepts all of those and
# routes to `logging.getLogger("agentalloy.code_index.engine")`. It keeps the
# vendored diff minimal: every loguru logger import was rewritten to import
# this shim instead.

import logging
from typing import Any

_stdlib_logger = logging.getLogger("agentalloy.code_index.engine")


class EngineLogger:
    """Small adapter accepting loguru-style formatting on stdlib logging."""

    __slots__ = ("_logger",)

    def __init__(self, backing: logging.Logger) -> None:
        self._logger = backing

    def _format(self, message: object, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
        text = str(message)
        if not args and not kwargs:
            return text
        if "{" in text:
            try:
                return text.format(*args, **kwargs)
            except (IndexError, KeyError, ValueError):
                pass
        elif args and "%" in text:
            try:
                return text % args
            except (TypeError, ValueError):
                pass
        # Fallback: append unconsumed context so nothing is silently dropped.
        extras = [str(a) for a in args] + [f"{k}={v}" for k, v in kwargs.items()]
        return f"{text} [{', '.join(extras)}]" if extras else text

    def _log(
        self, level: int, message: object, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        if self._logger.isEnabledFor(level):
            self._logger.log(level, self._format(message, args, kwargs))

    def trace(self, message: object, *args: Any, **kwargs: Any) -> None:
        self._log(logging.DEBUG, message, args, kwargs)

    def debug(self, message: object, *args: Any, **kwargs: Any) -> None:
        self._log(logging.DEBUG, message, args, kwargs)

    def info(self, message: object, *args: Any, **kwargs: Any) -> None:
        self._log(logging.INFO, message, args, kwargs)

    def success(self, message: object, *args: Any, **kwargs: Any) -> None:
        self._log(logging.INFO, message, args, kwargs)

    def warning(self, message: object, *args: Any, **kwargs: Any) -> None:
        self._log(logging.WARNING, message, args, kwargs)

    def error(self, message: object, *args: Any, **kwargs: Any) -> None:
        self._log(logging.ERROR, message, args, kwargs)

    def exception(self, message: object, *args: Any, **kwargs: Any) -> None:
        if self._logger.isEnabledFor(logging.ERROR):
            self._logger.error(self._format(message, args, kwargs), exc_info=True)

    def critical(self, message: object, *args: Any, **kwargs: Any) -> None:
        self._log(logging.CRITICAL, message, args, kwargs)


logger = EngineLogger(_stdlib_logger)
