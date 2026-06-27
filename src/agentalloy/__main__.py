"""Run the service: ``python -m agentalloy``."""

from __future__ import annotations

import uvicorn

from agentalloy.config import configure_logging, get_settings


def main() -> None:
    settings = get_settings()
    # Apply log_level to the agentalloy.* loggers before uvicorn starts.
    configure_logging(settings.log_level)
    uvicorn.run(
        "agentalloy.app:app",
        host="0.0.0.0",
        port=47950,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
