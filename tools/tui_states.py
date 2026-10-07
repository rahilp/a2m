"""Dev-only capture harness: open the a2m TUI in one named UI state.

    python tools/tui_states.py --state setup-ready
    textual serve --host 127.0.0.1 --port 8765 ".venv/bin/python tools/tui_states.py --state setup-ready"

Each state name matches a ``ui_states`` entry in the TUI run's checkpoints.json
and the prototype's ``?state=<name>`` route, and loads the same sample data
the prototype uses for that state (DESIGN.md section 9), so a capture of the
real app can be compared with the prototype screenshot of the same state.
States that need folders get real ones, built in a temporary folder that is
removed when the app exits: the prototype's 12 proxies and 2 shared flows, and
for an earlier run the results of 8 of them. Folder paths differ from the
prototype's ``/home/user/...`` only in their temporary parent folder.
Later steps add their states to ``STATES``. Not shipped with the package.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import tempfile
import types
from collections.abc import Callable
from pathlib import Path

# Run from a checkout without installing: make the repo's a2m package importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from a2m import layout
from a2m.ai.claude import KEY_ENV, SDK_MODULE
from a2m.engine import LlmChoice
from a2m.tui.app import A2MApp
from a2m.tui.command import SetupChoices

# The prototype's ALL_PROXIES (name, bucket), in its order, and its SHARED_FLOWS_FOUND = 2.
PROXIES: tuple[tuple[str, str], ...] = (
    ("orders-api", layout.VERIFIED_DIR_NAME),
    ("cart-api", layout.VERIFIED_DIR_NAME),
    ("payments-api", layout.VERIFIED_DIR_NAME),
    ("inventory-api", layout.VERIFIED_DIR_NAME),
    ("catalog-api", layout.VERIFIED_DIR_NAME),
    ("pricing-api", layout.VERIFIED_DIR_NAME),
    ("legacy-auth", layout.NEEDS_REVIEW_DIR_NAME),
    ("js-transform", layout.NEEDS_REVIEW_DIR_NAME),
    ("weather-api", layout.NEEDS_REVIEW_DIR_NAME),
    ("loyalty-api", layout.NEEDS_REVIEW_DIR_NAME),
    ("shipping-api", layout.UNSUPPORTED_DIR_NAME),
    ("returns-api", layout.UNSUPPORTED_DIR_NAME),
)
SHARED_FLOWS: tuple[str, ...] = ("common-auth", "common-logging")
# setup-existing-results: "8 of 12 proxies already have a .done marker from an earlier run".
EARLIER_DONE = 8

EXPORTS_NAME = "apigee-exports"
RESULTS_NAME = "a2m-out"
GOLDEN_NAME = "golden-recordings"
# setup-advanced: the prototype's Advanced values (mock backends on, 5 fix attempts, one ignored header).
ADVANCED_IGNORED_HEADER = "Authorization"
ADVANCED_FIX_ATTEMPTS = "5"
# setup-advanced has Claude usable: a stand-in key for this process only (never shown by the app), and a bare
# stand-in for the Anthropic SDK when it is not installed (the app only checks that it can be imported).
STAND_IN_KEY = "stand-in-key-for-tui-states"


def _write_exports(root: Path) -> Path:
    """The exports folder: one minimal folder bundle per prototype proxy and shared flow."""
    exports = root / EXPORTS_NAME
    for name, _bucket in PROXIES:
        bundle = exports / name / "apiproxy"
        bundle.mkdir(parents=True)
        (bundle / f"{name}.xml").write_text(
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<APIProxy revision="1" name="{name}"/>\n',
            encoding="utf-8",
        )
    for name in SHARED_FLOWS:
        bundle = exports / name / "sharedflowbundle"
        bundle.mkdir(parents=True)
        (bundle / f"{name}.xml").write_text(
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<SharedFlowBundle revision="1" name="{name}"/>\n',
            encoding="utf-8",
        )
    return exports


def _write_earlier_results(root: Path) -> Path:
    """A results folder an earlier run left: the a2m marker and the first proxies finished in their buckets."""
    out = root / RESULTS_NAME
    out.mkdir()
    layout.results_marker_path(out).write_text(layout.RESULTS_MARKER_TEXT, encoding="utf-8")
    for name, bucket in PROXIES[:EARLIER_DONE]:
        proxy_dir = layout.bucket_proxy_dir(out, bucket, name)
        proxy_dir.mkdir(parents=True)
        (proxy_dir / layout.DONE_MARKER_NAME).write_text(f"a2m finished {name}\n", encoding="utf-8")
    return out


def _setup_empty(root: Path) -> A2MApp:
    """CP3/CP4 setup-empty: the app just opened, nothing chosen yet (no fixture data needed)."""
    return A2MApp()


def _setup_ready(root: Path) -> A2MApp:
    """CP4/CP5 setup-ready: both folders valid (12 proxies, 2 shared flows; a new empty results folder), No AI
    chosen, Advanced closed."""
    exports = _write_exports(root)
    results = root / RESULTS_NAME
    results.mkdir()
    return A2MApp(exports=str(exports), results=str(results))


def _setup_invalid(root: Path) -> A2MApp:
    """CP4 setup-invalid: the results folder is inside the exports folder."""
    exports = _write_exports(root)
    return A2MApp(exports=str(exports), results=str(exports / "out"))


def _setup_existing_results(root: Path) -> A2MApp:
    """CP4 setup-existing-results: results from an earlier run (8 of 12 done); Resume or Force not picked yet."""
    exports = _write_exports(root)
    results = _write_earlier_results(root)
    return A2MApp(exports=str(exports), results=str(results))


def _setup_no_key(root: Path) -> A2MApp:
    """CP5 setup-no-key: both folders valid, Claude chosen without ANTHROPIC_API_KEY; message shown, Start disabled."""
    os.environ.pop(KEY_ENV, None)
    exports = _write_exports(root)
    results = root / RESULTS_NAME
    results.mkdir()
    return A2MApp(choices=SetupChoices(exports=str(exports), results=str(results), llm=LlmChoice.CLAUDE))


def _setup_advanced(root: Path) -> A2MApp:
    """CP5 setup-advanced: Claude usable, Advanced open with mock backends, a golden recordings folder, one
    ignored header and 5 AI fix attempts."""
    os.environ[KEY_ENV] = STAND_IN_KEY
    if SDK_MODULE not in sys.modules and importlib.util.find_spec(SDK_MODULE) is None:
        sys.modules[SDK_MODULE] = types.ModuleType(SDK_MODULE)
    exports = _write_exports(root)
    results = root / RESULTS_NAME
    results.mkdir()
    golden = root / GOLDEN_NAME
    golden.mkdir()
    choices = SetupChoices(
        exports=str(exports),
        results=str(results),
        llm=LlmChoice.CLAUDE,
        mock_backends=True,
        golden=str(golden),
        ignore_headers=ADVANCED_IGNORED_HEADER,
        max_fix_attempts=ADVANCED_FIX_ATTEMPTS,
    )
    return A2MApp(choices=choices, advanced_open=True)


STATES: dict[str, Callable[[Path], A2MApp]] = {
    "setup-empty": _setup_empty,
    "setup-ready": _setup_ready,
    "setup-invalid": _setup_invalid,
    "setup-existing-results": _setup_existing_results,
    "setup-no-key": _setup_no_key,
    "setup-advanced": _setup_advanced,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open the a2m TUI in one named UI state (dev capture harness).")
    parser.add_argument("--state", required=True, choices=sorted(STATES), help="the ui_states name to show")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="a2m-") as root:
        app = STATES[args.state](Path(root))
        app.run()
    return app.return_code or 0


if __name__ == "__main__":
    sys.exit(main())
