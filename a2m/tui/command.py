"""The ``a2m migrate`` command line that matches the setup screen's choices.

Built from the same :class:`~a2m.engine.RunOptions` a run from the screen uses, and quoted with
:func:`shlex.quote`, so the line shown can be pasted into a shell as it is. Imports no Textual.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from a2m.engine import DEFAULT_MAX_FIX_ATTEMPTS, LlmChoice, RunOptions, parse_whole_number

EXPORTS_PLACEHOLDER = "<exports folder>"
RESULTS_PLACEHOLDER = "<results folder>"
# --max-fix-attempts when the field is left as it is (the command line's own default): not written out.
DEFAULT_FIX_ATTEMPTS = DEFAULT_MAX_FIX_ATTEMPTS
# The ignored-headers field holds one or more header names; a header name never holds a comma or a space.
_HEADER_SEPARATORS = re.compile(r"[\s,]+")


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
    # The AI choice; No AI until the user picks Claude.
    llm: LlmChoice = LlmChoice.NONE
    # The Advanced options, as typed (blank: not given).
    only: str = ""
    mock_backends: bool = False
    golden: str = ""
    ignore_headers: str = ""
    max_fix_attempts: str = str(DEFAULT_FIX_ATTEMPTS)
    no_runtime: bool = False

    @property
    def exports_path(self) -> Path | None:
        return folder_path(self.exports)

    @property
    def results_path(self) -> Path | None:
        return folder_path(self.results)

    @property
    def golden_path(self) -> Path | None:
        return folder_path(self.golden)

    @property
    def only_name(self) -> str | None:
        """The one proxy to process (--only), or None for all of them."""
        return self.only.strip() or None

    @property
    def header_names(self) -> tuple[str, ...]:
        """The ignored header names typed, in order (one --golden-ignore-header each)."""
        return tuple(name for name in _HEADER_SEPARATORS.split(self.ignore_headers) if name)

    @property
    def fix_attempts_text(self) -> str:
        """--max-fix-attempts as typed ("" when left blank, which means the default)."""
        return self.max_fix_attempts.strip()

    def run_options(self) -> RunOptions | None:
        """The options a run with these choices uses; None until both folders are given and the number is valid."""
        exports, results = self.exports_path, self.results_path
        if exports is None or results is None:
            return None
        try:
            attempts = parse_whole_number(self.fix_attempts_text) if self.fix_attempts_text else DEFAULT_FIX_ATTEMPTS
        except ValueError:
            return None
        return RunOptions(
            input_dir=exports,
            out_dir=results,
            only=self.only_name,
            resume=self.rerun is RerunChoice.RESUME,
            force=self.rerun is RerunChoice.FORCE,
            golden=self.golden_path,
            mock_backends=self.mock_backends,
            max_fix_attempts=attempts,
            llm=self.llm,
            no_runtime=self.no_runtime,
            golden_ignore_headers=self.header_names,
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
    argv += ["--llm", choices.llm.value]
    only = choices.only_name
    if only is not None:
        # A name starting with "-" is joined to its option, so it is not read as an option itself.
        argv += [f"--only={only}"] if only.startswith("-") else ["--only", only]
    if choices.mock_backends:
        argv.append("--mock-backends")
    golden = choices.golden_path
    if golden is not None:
        argv += ["--golden", _path_arg(golden)]
    for name in choices.header_names:
        argv += [f"--golden-ignore-header={name}"] if name.startswith("-") else ["--golden-ignore-header", name]
    attempts = choices.fix_attempts_text
    if attempts and attempts != str(DEFAULT_FIX_ATTEMPTS):
        argv += [f"--max-fix-attempts={attempts}"] if attempts.startswith("-") else ["--max-fix-attempts", attempts]
    if choices.no_runtime:
        argv.append("--no-runtime")
    return argv


def _path_arg(path: Path) -> str:
    """``path`` as a command-line argument: a relative path starting with ``-`` gets ``./`` so it is not an option."""
    text = str(path)
    return f".{os.sep}{text}" if text.startswith("-") else text


def command_preview(choices: SetupChoices | None = None) -> str:
    """The shell-quoted ``a2m migrate`` line for ``choices`` (nothing chosen when None)."""
    return shlex.join(migrate_argv(choices or SetupChoices()))
