"""Check the setup screen's choices with the engine's own checks; read-only, imports no Textual.

Every message shown for a folder or option that cannot be used is the one ``a2m migrate`` gives for it
(:func:`a2m.engine.check_exports`, :func:`a2m.engine.check_results_folder`, the Advanced options' checks and,
for Claude, :func:`a2m.ai.claude.setup_problem`), so the screen and the command line never disagree. Nothing
here creates, changes or deletes anything. The Claude check reads only whether ANTHROPIC_API_KEY is set,
never its value.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from a2m.ai.claude import SetupProblem, setup_problem_kind
from a2m.engine import (
    LlmChoice,
    ResultsFolder,
    ResultsFolderInUse,
    check_exports,
    check_golden,
    check_only,
    check_results_folder,
    done_proxies,
    parse_header_name,
    parse_whole_number,
)
from a2m.errors import UsageError
from a2m.tui.command import RerunChoice, SetupChoices
from a2m.tui.read import Problem, preview_label

VALID_MARK = "✓"
INVALID_MARK = "✗"


class Status(StrEnum):
    """How a folder field's check ended."""

    NONE = "none"  # nothing typed yet: no line shown
    VALID = "valid"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class FieldCheck:
    status: Status = Status.NONE
    # The one line shown under the field ("" with Status.NONE).
    text: str = ""

    @classmethod
    def valid(cls, text: str) -> FieldCheck:
        return cls(Status.VALID, text)

    @classmethod
    def invalid(cls, message: str) -> FieldCheck:
        return cls(Status.INVALID, f"{INVALID_MARK} {message}")


@dataclass(frozen=True, slots=True)
class SetupCheck:
    """The result of checking both folders for one set of choices."""

    choices: SetupChoices
    exports: FieldCheck
    results: FieldCheck
    # What the results folder holds, when it is usable or only in use by another run right now
    # (None: not given or refused).
    results_folder: ResultsFolder | None = None
    # The refusal of the user's Resume or Force pick for that folder, e.g. --resume on a force-pending folder.
    rerun_error: str | None = None
    # Why the AI choice cannot run (Claude without its key or SDK), and the first Advanced option that cannot.
    ai: FieldCheck = FieldCheck()
    advanced: FieldCheck = FieldCheck()
    # Which of the AI problems ``ai`` shows (None: the AI choice can run), so callers never parse its text.
    ai_problem: SetupProblem | None = None

    @property
    def needs_rerun_choice(self) -> bool:
        """The results folder holds an earlier run, so the user must pick Resume or Force."""
        return self.results_folder in (ResultsFolder.EARLIER_RUN, ResultsFolder.FORCE_PENDING)

    @property
    def ready(self) -> bool:
        """Both folders are usable and nothing is left to choose: a run could start."""
        if self.exports.status is not Status.VALID or self.results.status is not Status.VALID:
            return False
        if Status.INVALID in (self.ai.status, self.advanced.status):
            return False
        if self.needs_rerun_choice:
            return self.choices.rerun is not None and self.rerun_error is None
        return True


def check_setup(choices: SetupChoices) -> SetupCheck:
    """Check the folders, the AI choice and the Advanced options of ``choices`` as ``a2m migrate`` would; read-only."""
    exports = choices.exports_path
    names: list[str] = []
    exports_check = FieldCheck()
    if exports is not None:
        exports_check, names = _check_exports(exports)
    found = _check_folders(choices, exports_check, names)
    checked = exports if exports_check.status is Status.VALID else None
    problem = ai_problem(choices.llm)
    return replace(
        found,
        ai=_ai_field(problem),
        ai_problem=problem,
        advanced=check_advanced(choices, names, checked),
    )


def ai_problem(llm: LlmChoice) -> SetupProblem | None:
    """Why the AI choice could not run (Claude without ANTHROPIC_API_KEY or its SDK), or None when it could."""
    return setup_problem_kind() if llm is LlmChoice.CLAUDE else None


def check_ai(llm: LlmChoice) -> FieldCheck:
    """Why the AI choice could not run, in a2m's own words (Claude without ANTHROPIC_API_KEY or its SDK)."""
    return _ai_field(ai_problem(llm))


def _ai_field(problem: SetupProblem | None) -> FieldCheck:
    return FieldCheck.invalid(problem.message) if problem is not None else FieldCheck()


def check_advanced(choices: SetupChoices, names: Sequence[str] = (), exports: Path | None = None) -> FieldCheck:
    """The first Advanced option ``a2m migrate`` would refuse, with its message; ``names`` (the proxies found in
    ``exports``) check the one proxy to process, when given."""
    try:
        check_golden(None if choices.golden_path is None else choices.golden_path.absolute())
    except UsageError as exc:
        return FieldCheck.invalid(str(exc))
    for name in choices.header_names:
        try:
            parse_header_name(name)
        except ValueError as exc:
            return FieldCheck.invalid(f"argument --golden-ignore-header: {exc}")
    if choices.fix_attempts_text:
        try:
            parse_whole_number(choices.fix_attempts_text)
        except ValueError as exc:
            return FieldCheck.invalid(f"argument --max-fix-attempts: {exc}")
    if exports is not None and names:
        try:
            check_only(choices.only_name, names, exports.absolute())
        except UsageError as exc:
            return FieldCheck.invalid(str(exc))
    return FieldCheck()


def _check_folders(choices: SetupChoices, exports_check: FieldCheck, names: list[str]) -> SetupCheck:
    """Check the results folder of ``choices`` (the exports folder was checked already, finding ``names``)."""
    exports = choices.exports_path
    results = choices.results_path
    if results is None:
        return SetupCheck(choices, exports_check, FieldCheck())

    # The lock is checked last and never hides what the folder holds: a locked earlier-run or force-pending
    # folder still offers Resume and Force, and the pick is checked again (lock last) below.
    in_use: str | None = None
    try:
        found = check_results_folder(results, input_dir=exports, names=names)
    except ResultsFolderInUse as exc:
        found, in_use = exc.found, str(exc)
    except UsageError as exc:
        return SetupCheck(choices, exports_check, FieldCheck.invalid(str(exc)))
    if found is ResultsFolder.NEW:
        if in_use is not None:
            return SetupCheck(choices, exports_check, FieldCheck.invalid(in_use))
        ready = "Ready" if results.exists() else "Ready, a2m will create this folder"
        return SetupCheck(choices, exports_check, FieldCheck.valid(f"{VALID_MARK} {ready}"), found)

    done = len(done_proxies(results, names))
    if names:
        line = f"{done} of {len(names)} proxies already have a .done marker from an earlier run"
    else:
        line = "This folder has output from an earlier a2m run"
    rerun_error = None
    if choices.rerun is not None:
        try:
            check_results_folder(
                results,
                input_dir=exports,
                names=names,
                resume=choices.rerun is RerunChoice.RESUME,
                force=choices.rerun is RerunChoice.FORCE,
            )
        except UsageError as exc:
            rerun_error = str(exc)
    # With no pick yet the field shows the lock; once picked, the pick's own check (lock last) decides.
    refusal = rerun_error if choices.rerun is not None else in_use
    results_check = FieldCheck.invalid(refusal) if refusal is not None else FieldCheck.valid(line)
    return SetupCheck(choices, exports_check, results_check, found, rerun_error)


def preview_exports(folder: Path) -> FieldCheck:
    """What ``a2m migrate`` would find in ``folder`` as the exports folder (the folder browser's preview); read-only."""
    return _check_exports(folder)[0]


def preview_results(folder: Path, *, exports: Path | None) -> FieldCheck:
    """Whether ``folder`` could hold a run's results, with the engine's own checks; read-only.

    For the folder browser's preview: empty, an earlier a2m results folder, or the message ``a2m migrate``
    gives for a folder it refuses. ``exports`` (the exports field, if any) catches a folder inside it.
    """
    try:
        found = check_results_folder(folder, input_dir=exports, names=())
    except ResultsFolderInUse as exc:
        # In use right now: say so, and still say what the folder holds (the lock never hides it).
        if exc.found is ResultsFolder.NEW:
            return FieldCheck.invalid(str(exc))
        return FieldCheck.invalid(f"{exc}. {_earlier_run_text(exc.found)}")
    except UsageError as exc:
        return FieldCheck.invalid(str(exc))
    if found is ResultsFolder.NEW:
        return FieldCheck.valid(f"{VALID_MARK} No results yet, ready for a new run")
    return FieldCheck.valid(_earlier_run_text(found))


def preview_open_results(folder: Path) -> FieldCheck:
    """Whether ``folder`` holds a2m results the results screen can show (the folder browser's preview when
    opening results); read-only."""
    problem, text = preview_label(folder)
    if problem is None:
        return FieldCheck.valid(f"{VALID_MARK} {text}")
    if problem is Problem.NO_SUMMARY:
        return FieldCheck(Status.NONE, text)
    return FieldCheck.invalid(text)


def _earlier_run_text(found: ResultsFolder) -> str:
    """The preview line for a results folder holding an earlier run (EARLIER_RUN or FORCE_PENDING)."""
    if found is ResultsFolder.FORCE_PENDING:
        return "Results of an earlier a2m run whose --force redo stopped; only Force can continue it"
    return "Results of an earlier a2m run; you can resume it or redo every proxy"


def _check_exports(exports: Path) -> tuple[FieldCheck, list[str]]:
    """The exports field's line for ``exports`` and the proxies found there."""
    try:
        discovery = check_exports(exports)
    except UsageError as exc:
        return FieldCheck.invalid(str(exc)), []
    names = discovery.candidate_names()
    return FieldCheck.valid(_found_line(len(names), len(discovery.shared_flows), len(discovery.rejected))), names


def _found_line(proxies: int, shared_flows: int, refused: int) -> str:
    proxy_word = "proxy" if proxies == 1 else "proxies"
    flow_word = "shared flow" if shared_flows == 1 else "shared flows"
    line = f"{VALID_MARK} {proxies} {proxy_word}, {shared_flows} {flow_word} found"
    if refused:
        line += f"; {refused} of them will be refused (see run.log after the run)"
    return line
