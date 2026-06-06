"""Run benchmarks via: python -m eval [layer_nums...]"""

from __future__ import annotations

import sys

from eval.benchmark import main

if __name__ == "__main__":
    sys.exit(main())
