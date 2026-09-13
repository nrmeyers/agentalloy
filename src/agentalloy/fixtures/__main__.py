"""Fixture loader CLI: ``python -m agentalloy.fixtures load``."""

from __future__ import annotations

import argparse
import logging
import sys

from agentalloy.config import get_settings
from agentalloy.fixtures.loader import load_fixtures
from agentalloy.storage.open import open_skills


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentalloy.fixtures")
    parser.add_argument("command", choices=["load"])
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    settings = get_settings()
    settings.ensure_data_dirs()

    if args.command == "load":
        store = open_skills(settings, read_only=False)
        try:
            load_fixtures(store)
        finally:
            store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
