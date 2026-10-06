"""``python -m a2m.tui``: open the terminal UI directly (used by ``textual serve``)."""

from __future__ import annotations

import sys

from a2m.tui.app import run_app

if __name__ == "__main__":
    # textual serve drives the app over pipes (its own web driver), so no terminal check here.
    sys.exit(run_app(at_terminal=lambda: True))
