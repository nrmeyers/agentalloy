"""Run the service: ``python -m agentalloy``."""

from __future__ import annotations

import logging

import uvicorn

from agentalloy.config import get_settings


def main() -> None:
    settings = get_settings()
    # Apply log_level to the root logger so logging is configured before uvicorn starts.
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        "agentalloy.app:app",
        host="0.0.0.0",
        port=47950,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
