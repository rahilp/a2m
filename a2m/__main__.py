"""``python -m a2m``: the same command line as the ``a2m`` console script."""

from __future__ import annotations

import sys

from a2m.cli import main

if __name__ == "__main__":
    sys.exit(main())
