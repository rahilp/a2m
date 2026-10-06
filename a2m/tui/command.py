"""The ``a2m migrate`` command line that matches the setup screen's choices.

Built from the same :class:`~a2m.engine.RunOptions` a run from the screen uses, and quoted with
:func:`shlex.quote`, so the line shown can be pasted into a shell as it is. Imports no Textual.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from a2m.engine import RunOptions

EXPORTS_PLACEHOLDER = "<exports folder>"
RESULTS_PLACEHOLDER = "<results folder>"


class RerunChoice(StrEnum):
    """What to do with a results folder that holds an earlier run."""

    RESUME = "resume"
    FORCE = "force"


@dataclass(frozen=True, slots=True)
class SetupChoices:
    """What the user has typed or picked on the setup screen so far."""

    exports: str = ""
    results: str = ""
    rerun: RerunChoice | None = None

    @property
    def exports_path(self) -> Path | None:
        return folder_path(self.exports)

    @property
    def results_path(self) -> Path | None:
        return folder_path(self.results)

    def run_options(self) -> RunOptions | None:
        """The options a run with these choices uses; None until both folders are given."""
        exports, results = self.exports_path, self.results_path
        if exports is None or results is None:
            return None
        return RunOptions(
            input_dir=exports,
            out_dir=results,
            resume=self.rerun is RerunChoice.RESUME,
            force=self.rerun is RerunChoice.FORCE,
        )


def folder_path(text: str) -> Path | None:
    """The folder a field's text names (a leading ``~`` is the home folder), or None when it is blank."""
    if not text.strip():
        return None
    return Path(text).expanduser()


def resolved_folder(text: str) -> Path | None:
    """The absolute folder a field's text names, relative to the current folder (None when blank).

    Purely lexical (``~`` expanded, ``.`` and ``..`` folded), so it reads nothing from disk.
    """
    path = folder_path(text)
    return None if path is None else Path(os.path.abspath(path))


def migrate_argv(choices: SetupChoices) -> list[str]:
    """``a2m migrate`` and its arguments for ``choices``, with a placeholder for each folder not given yet."""
    exports = choices.exports_path
    results = choices.results_path
    argv = [
        "a2m",
        "migrate",
        _path_arg(exports) if exports is not None else EXPORTS_PLACEHOLDER,
        "--out",
        _path_arg(results) if results is not None else RESULTS_PLACEHOLDER,
    ]
    if choices.rerun is RerunChoice.RESUME:
        argv.append("--resume")
    if choices.rerun is RerunChoice.FORCE:
        argv.append("--force")
    return argv


def _path_arg(path: Path) -> str:
    """``path`` as a command-line argument: a relative path starting with ``-`` gets ``./`` so it is not an option."""
    text = str(path)
    return f".{os.sep}{text}" if text.startswith("-") else text


def command_preview(choices: SetupChoices | None = None) -> str:
    """The shell-quoted ``a2m migrate`` line for ``choices`` (nothing chosen when None)."""
    return shlex.join(migrate_argv(choices or SetupChoices()))
