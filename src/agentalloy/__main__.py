"""CLI entry point: `python -m agentalloy` delegates to the package CLI."""

import sys

from agentalloy.cli import main

if __name__ == "__main__":
    sys.exit(main())
