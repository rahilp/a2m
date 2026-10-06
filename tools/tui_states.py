"""Dev-only capture harness: open the a2m TUI in one named UI state.

    python tools/tui_states.py --state setup-empty
    textual serve --host 127.0.0.1 --port 8765 ".venv/bin/python tools/tui_states.py --state setup-empty"

Each state name matches a ``ui_states`` entry in the TUI run's checkpoints.json
and the prototype's ``?state=<name>`` route, and loads the same sample data
the prototype uses for that state (DESIGN.md section 9), so a capture of the
real app can be compared with the prototype screenshot of the same state.
Later steps add their states to ``STATES``. Not shipped with the package.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

# Run from a checkout without installing: make the repo's a2m package importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from a2m.tui.app import A2MApp


def _setup_empty() -> A2MApp:
    """CP3/CP4 setup-empty: the app just opened, nothing chosen yet (no fixture data needed)."""
    return A2MApp()


STATES: dict[str, Callable[[], A2MApp]] = {
    "setup-empty": _setup_empty,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open the a2m TUI in one named UI state (dev capture harness).")
    parser.add_argument("--state", required=True, choices=sorted(STATES), help="the ui_states name to show")
    args = parser.parse_args(argv)
    app = STATES[args.state]()
    app.run()
    return app.return_code or 0


if __name__ == "__main__":
    sys.exit(main())
