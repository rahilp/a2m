"""The batch engine: check the run, then process each proxy through the stages.

A run has two phases. :func:`prepare_run` checks the input folder, the results
folder and the flags, and discovers bundles, without writing anything; it
raises :class:`UsageError` for anything the user must fix. :func:`run_batch`
then creates the results folder, writes run.log and runs every selected proxy
through the per-proxy stages. A proxy that raises is logged and the batch
carries on; its ``.done`` marker is written only after every stage succeeded.

Stages never see the input folder. Before any stage runs, every bundle, folder
or zip, is materialized into a sanitized working copy under the results
folder's work area (:func:`_materialize`): only plain files and real folders,
within fixed limits, never through a link. A bundle that cannot be copied that
way is refused. :class:`ProxyContext` carries that copy and no path into the
input folder.

Every delete and overwrite under the results folder goes through
:mod:`a2m.safefs`, which refuses symbolic links on the way and anything not
strictly inside the results folder.

While a batch runs, SIGTERM and SIGHUP raise :class:`Terminated` (a
KeyboardInterrupt, like Ctrl-C's SIGINT), so the batch ends through the same
path as Ctrl-C and every stage is closed: the Mule runtime the verification
stage started is stopped even when the run is killed with ``kill``,
``timeout`` or a closed terminal. A signal the caller set to be ignored
(``nohup``) stays ignored.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import os
import shlex
import signal
import stat
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from a2m import layout, pathid, safefs
from a2m.ai import Provider, ProviderSetupError, make_provider
from a2m.ai.prompts import FIX_PROMPT_FILE, PromptError, load_prompt, load_prompts
from a2m.discovery import (
    BUNDLE_ROOTS,
    PROXY_ROOT,
    BundleRoot,
    BundleSource,
    Discovery,
    RejectedItem,
    SourceKind,
    bundle_root,
    copy_folder_bundle,
    discover,
    extract_zip,
    folder_subfolders,
)
from a2m.errors import (
    BundleError,
    LockHeldError,
    NotPlainFileError,
    UnsafeBundleError,
    UnsafePathError,
    UsageError,
)
from a2m.generator import GenerateResult, generate_project
from a2m.inventory import GenerateRecords
from a2m.ir import Bundle
from a2m.layout import collision_key, unsafe_name_reason
from a2m.parser import read_bundle
from a2m.policies.common import Method
from a2m.progress import Outcome, Progress, ProgressCallback
from a2m.report import ReportStage
from a2m.runlog import get_logger, run_log
from a2m.verify import make_verify_stage
from a2m.verify.generated import GeneratedSteps

# Names shown when --only does not match; longer lists are cut short.
MAX_NAMES_IN_MESSAGE = 20


class LlmChoice(StrEnum):
    CLAUDE = "claude"
    FAKE = "fake"


@dataclass(frozen=True, slots=True)
class RunOptions:
    input_dir: Path
    out_dir: Path
    only: str | None = None
    resume: bool = False
    force: bool = False
    golden: Path | None = None
    mock_backends: bool = False
    max_fix_attempts: int = 3
    llm: LlmChoice = LlmChoice.CLAUDE
    no_runtime: bool = False
    # Extra header names a golden replay does not compare (--golden-ignore-header), on top of the defaults.
    golden_ignore_headers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StageOptions:
    """The run options a per-proxy stage may use; no input or results paths."""

    golden: Path | None = None
    mock_backends: bool = False
    max_fix_attempts: int = 3
    llm: LlmChoice = LlmChoice.CLAUDE
    no_runtime: bool = False
    golden_ignore_headers: tuple[str, ...] = ()
    # The AI provider --llm picked (checked by prepare_run); None in stages built without one.
    provider: Provider | None = field(default=None, compare=False, repr=False)

    @classmethod
    def of(cls, options: RunOptions, provider: Provider | None = None) -> StageOptions:
        return cls(
            golden=options.golden,
            mock_backends=options.mock_backends,
            max_fix_attempts=options.max_fix_attempts,
            llm=options.llm,
            no_runtime=options.no_runtime,
            golden_ignore_headers=options.golden_ignore_headers,
            provider=provider,
        )


@dataclass(frozen=True, slots=True)
class ProxyContext:
    """What each per-proxy stage receives.

    ``bundle_dir`` is the folder that holds the bundle root (a proxy's files
    are under ``bundle_dir / "apiproxy"``) in the sanitized working copy under
    the results folder's work area, never a path in the input folder;
    ``source_name`` is the input item's file name, for reports only.
    ``shared_flows`` are the shared flow bundles of the input folder that could
    be read, for the proxy's FlowCallout steps.
    """

    name: str
    source_name: str
    kind: SourceKind
    bundle_dir: Path
    out_dir: Path
    options: StageOptions
    shared_flows: tuple[Bundle, ...] = ()


Stage = Callable[[ProxyContext], None]


def parse(context: ProxyContext) -> None:
    """Read the proxy's bundle into the IR; a bundle that cannot be read raises BundleError, so it is refused."""
    bundle = read_bundle(context.bundle_dir, label=context.name)
    get_logger().info(
        "%s: read %d proxy endpoints, %d target endpoints, %d policies, %d resources",
        context.name,
        len(bundle.proxy_endpoints),
        len(bundle.target_endpoints),
        len(bundle.policies),
        len(bundle.resources),
    )


def generate(context: ProxyContext) -> None:
    """Write the proxy's Mule project into <proxy>/mule-app; log everything that was not generated."""
    log = get_logger()
    bundle = read_bundle(context.bundle_dir, label=context.name)
    dest = layout.mule_app_dir(context.out_dir)
    result = generate_project(
        bundle,
        dest,
        shared_flows=context.shared_flows,
        results_root=context.out_dir,
        provider=context.options.provider,
    )
    log.info("%s: wrote Mule project %s/ (%d files)", context.name, layout.MULE_APP_DIR_NAME, len(result.files))
    _save_generated_steps(context, result)
    if result.requires_enterprise:
        log.warning(
            "%s: the Mule app requires a Mule Enterprise runtime: it uses %s (in steps %s), which Mule Kernel "
            "(Community Edition) does not have",
            context.name,
            ", ".join(result.enterprise_components),
            ", ".join(result.enterprise_steps) or "-",
        )
    for item in result.unsupported:
        log.warning("%s: not generated: %s: %s", context.name, item.name, item.reason)
    for policy in result.policies:
        if policy.method is Method.AI:
            (log.warning if policy.needs_review else log.info)(
                "%s: step %s (%s) sent to the AI: %s, confidence %s, %s; notes: %s",
                context.name,
                policy.name,
                policy.type,
                "needs review" if policy.needs_review else "translated",
                policy.confidence.value if policy.confidence is not None else "none",
                policy.reason or "no review reason",
                policy.notes or "-",
            )
    for record in result.conditions:
        if record.method is Method.AI:
            (log.warning if record.needs_review else log.info)(
                "%s: %s %s condition sent to the AI: %s, confidence %s; notes: %s; condition: %s",
                context.name,
                record.kind,
                record.name,
                f"needs review ({record.reason})" if record.needs_review else f"translated as {record.dw}",
                record.confidence.value if record.confidence is not None else "none",
                record.notes or "-",
                record.original,
            )
        elif record.ok:
            log.info("%s: %s %s condition translated: %s", context.name, record.kind, record.name, record.original)
        else:
            log.warning(
                "%s: %s %s condition can't be translated (%s), so it never runs: %s",
                context.name,
                record.kind,
                record.name,
                record.reason,
                record.original,
            )


def _save_generated_steps(context: ProxyContext, result: GenerateResult) -> None:
    """Keep what the generator made of each step in the proxy's work folder, for the verification stage (which
    tests only the steps a2m generated and that run in the app; see a2m.verify.generated), and the generator's full
    records for the report stage (see a2m.inventory; written whether or not a report stage runs)."""
    results_root = context.out_dir.parent
    path = layout.generated_steps_path(results_root, context.name)
    try:
        safefs.make_dirs(results_root, path.parent)
        safefs.write_text_atomic(results_root, path, GeneratedSteps.from_result(result).to_json())
    except OSError as exc:
        get_logger().warning(
            "%s: could not save the generated steps for verification (%s); it uses a2m's own decisions instead",
            context.name,
            exc,
        )
    records = layout.generate_records_path(results_root, context.name)
    try:
        safefs.write_text_atomic(results_root, records, GenerateRecords.from_result(result).to_json())
    except OSError as exc:
        get_logger().warning("%s: could not save the generator's records for the report (%s)", context.name, exc)


def default_stages() -> tuple[Stage, ...]:
    """The per-proxy pipeline of one batch (tests replace it through ``stages=``).

    Made per batch: the verification stage keeps the batch's Mule runtime, started
    on first use and stopped when the batch ends (see :func:`run_batch`). The
    report stage writes each proxy's REPORT.md and picks its bucket; with it in
    the list, each proxy ends in ``<bucket>/<proxy>/`` and the batch gets
    SUMMARY.md and summary.json (see :func:`_reporter`).
    """
    return (parse, generate, make_verify_stage(), ReportStage())


class Reporter(Protocol):
    """A stage that sorts proxies into buckets (found by duck typing: see :func:`_reporter`)."""

    def bucket_for(self, name: str) -> str | None: ...

    def write_unsupported(
        self,
        out: Path,
        name: str,
        source: str,
        cause: str,
        *,
        refused: bool,
        bundle_dir: Path | None = None,
        roots: Sequence[tuple[Path, str]] = (),
    ) -> Path: ...

    def finish_batch(
        self, out: Path, extra: Sequence[tuple[str, str]] = (), roots: Sequence[tuple[Path, str]] = ()
    ) -> None: ...


def _reporter(stages: Sequence[Stage]) -> Reporter | None:
    """The first stage that sorts proxies into buckets (it has ``bucket_for``, ``write_unsupported`` and
    ``finish_batch``), or None. Without one (custom stage lists), each proxy's folder stays ``<results>/<proxy>/``
    and no summary is written."""
    for stage in stages:
        if all(callable(getattr(stage, attr, None)) for attr in ("bucket_for", "write_unsupported", "finish_batch")):
            return stage  # type: ignore[return-value]
    return None


class Terminated(KeyboardInterrupt):
    """SIGTERM or SIGHUP during a batch; ``signum`` is the signal. Handled like Ctrl-C, so the batch cleans up."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"stopped by {signal.Signals(signum).name}")
        self.signum = signum


@contextlib.contextmanager
def _stop_on_termination() -> Iterator[None]:
    """For the block, make SIGTERM and SIGHUP raise :class:`Terminated` (main thread only; ignored stays ignored).

    Only the first signal raises; later ones are ignored, so the clean-up it starts is not cut short.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    raised = False

    def handler(signum: int, frame: object) -> None:
        nonlocal raised
        if raised:
            return
        raised = True
        raise Terminated(signum)

    previous: dict[signal.Signals, object] = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            current = signal.getsignal(sig)
            if current == signal.SIG_IGN:
                continue
            previous[sig] = signal.signal(sig, handler)
        except (OSError, ValueError):
            continue
    try:
        yield
    finally:
        for sig, old in previous.items():
            with contextlib.suppress(OSError, ValueError, TypeError):
                signal.signal(sig, old if old is not None else signal.SIG_DFL)  # type: ignore[arg-type]


def interrupted_signal(exc: BaseException) -> int | None:
    """The signal number when ``exc`` (or what it was raised from) is a :class:`Terminated`, else None."""
    for item in (exc, exc.__cause__, exc.__context__):
        if isinstance(item, Terminated):
            return item.signum
    return None


class RunInterrupted(KeyboardInterrupt):
    """Ctrl-C during a run, carrying the rerun advice that finishes the job (see :func:`rerun_advice`)."""

    def __init__(self, advice: str) -> None:
        super().__init__(advice)
        self.advice = advice


def rerun_advice(options: RunOptions, exc: BaseException | None = None) -> str:
    """What to run after ``exc`` interrupted a run with ``options``: the flags that finish the job.

    --resume trusts every ``.done`` marker. A --force run clears the markers of
    every selected proxy before it redoes the first one, so once that is done
    --resume finishes it; before that (and before the run started at all)
    only --force does. ``exc`` is a :class:`RunInterrupted` when the run got
    far enough to say which; any other interrupt counts as before.
    """
    if isinstance(exc, RunInterrupted):
        return exc.advice
    return _advice(options, cleared=not options.force)


def _advice(options: RunOptions, *, cleared: bool) -> str:
    quoted = shlex.quote(options.only) if options.only is not None else ""
    only = "" if options.only is None else f" --only={quoted}" if quoted.startswith("-") else f" --only {quoted}"
    if cleared:
        return f"rerun with --resume{only} to continue"
    return f"rerun with --force{only} to redo every proxy"


@dataclass(frozen=True, slots=True)
class RunPlan:
    options: RunOptions
    discovery: Discovery
    selected: tuple[BundleSource, ...]
    selected_rejected: tuple[RejectedItem, ...]
    provider: Provider | None = field(default=None, compare=False, repr=False)


@dataclass(slots=True)
class BatchResult:
    """What happened to each selected input item: every one lands in exactly one list.

    ``finished``, ``skipped``, ``refused`` and ``crashed`` together hold one
    entry per item in ``RunPlan.selected`` plus ``RunPlan.selected_rejected``
    (a name appears once per input item, so two conflicting items named
    ``alpha`` give two entries). A refused item whose earlier results could
    not be removed is recorded only in ``crashed`` (failed, exit 1): its stale
    ``.done`` would otherwise let a later --resume skip a fixed bundle, so the
    user must act on it. Likewise a --force proxy whose earlier ``.done``
    could not be removed is not run and is recorded only in ``crashed``; so
    is an unselected proxy folder whose marker such a run had to remove (an
    earlier --force run left its force-pending marker) and could not.
    """

    finished: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    crashed: list[str] = field(default_factory=list)
    log_path: Path | None = None
    # One line per thing the whole batch could not do (e.g. verification skipped: a tool is not installed).
    notices: list[str] = field(default_factory=list)


def prepare_run(options: RunOptions) -> RunPlan:
    """Check everything that can be checked before writing; raise UsageError on problems.

    Every path the user gave (EXPORTS, --out, --golden) is made absolute here, once, so nothing later
    depends on the working folder (the Mule launcher, for one, runs from its own folder).
    """
    options = _absolute_paths(options)
    if options.resume and options.force:
        raise UsageError("--resume and --force cannot be used together")
    if options.max_fix_attempts < 0:
        raise UsageError("--max-fix-attempts must be 0 or greater")
    _check_golden(options)
    provider = _make_provider(options)
    discovery = discover(options.input_dir)
    names = discovery.candidate_names()
    if not names:
        _check_not_a_bundle(options.input_dir)
    _check_out_dir(options)

    _check_done_markers(options.out_dir, names)
    if not names:
        raise UsageError(_no_proxies_message(options.input_dir, discovery))
    if options.only is not None and options.only not in names:
        shown = ", ".join(names[:MAX_NAMES_IN_MESSAGE])
        more = len(names) - MAX_NAMES_IN_MESSAGE
        if more > 0:
            shown += f" and {more} more"
        raise UsageError(
            f"no proxy named {options.only} was found in {options.input_dir}; proxies found: {shown}"
        )

    def wanted(name: str) -> bool:
        return options.only is None or name == options.only

    return RunPlan(
        options=options,
        discovery=discovery,
        selected=tuple(p for p in discovery.proxies if wanted(p.name)),
        selected_rejected=tuple(r for r in discovery.rejected if wanted(r.name)),
        provider=provider,
    )


def _no_proxies_message(input_dir: Path, discovery: Discovery) -> str:
    shared = f" ({len(discovery.shared_flows)} shared flow bundles only)" if discovery.shared_flows else ""
    return (
        f"no proxies found in {input_dir}{shared}; expected folders or .zip files "
        f"with an {PROXY_ROOT}/ folder at the top, or inside one top folder"
    )


class ResultsFolder(StrEnum):
    """What a usable results folder holds, as far as starting a run in it goes."""

    # Missing, empty, or holding only the lock file: a run starts fresh.
    NEW = "new"
    # Output from an earlier run: --resume continues it, --force redoes every proxy.
    EARLIER_RUN = "earlier-run"
    # An earlier --force run stopped before it removed every earlier .done marker: only --force can continue.
    FORCE_PENDING = "force-pending"


class ResultsFolderInUse(UsageError):
    """:func:`check_results_folder`'s refusal of a folder another run holds locked, with what the folder holds.

    The lock is checked last, so ``found`` is what the folder holds (an earlier run, or a force-pending one)
    and a front end can still offer the Resume or Force choice the folder needs once it is free.
    """

    def __init__(self, message: str, found: ResultsFolder) -> None:
        super().__init__(message)
        self.found = found


def check_exports(input_dir: Path) -> Discovery:
    """The checks :func:`prepare_run` makes of the exports folder alone, with the same messages; read-only.

    For front ends that check each folder as the user picks it (``a2m tui``). Returns what discovery
    found; raises :class:`UsageError` when the folder is missing, is itself a bundle or holds no proxy.
    """
    input_dir = input_dir.absolute()
    discovery = discover(input_dir)
    if not discovery.candidate_names():
        _check_not_a_bundle(input_dir)
        raise UsageError(_no_proxies_message(input_dir, discovery))
    return discovery


def check_results_folder(
    out_dir: Path,
    *,
    input_dir: Path | None,
    names: Sequence[str],
    resume: bool = False,
    force: bool = False,
) -> ResultsFolder:
    """The checks ``a2m migrate`` makes of a results folder, with the same messages; read-only.

    For front ends that check the results folder as the user picks it (``a2m tui``), before the user has
    chosen --resume or --force: an earlier run's folder is returned as :attr:`ResultsFolder.EARLIER_RUN` or
    :attr:`ResultsFolder.FORCE_PENDING` instead of being refused. Raises :class:`UsageError` for every
    folder ``a2m migrate`` would refuse, and for ``resume`` on a force-pending folder. A folder another run
    holds locked (checked last, without taking the lock) raises :class:`ResultsFolderInUse`, which still
    carries what the folder holds, so the lock never hides an earlier or force-pending run. ``input_dir`` None skips the input folder
    check; ``names`` are the proxies found in it (their ``.done`` markers must be plain files).
    Nothing is created, changed or deleted.
    """
    # The checks run in the order a2m migrate makes them (prepare_run, then run_batch's lock), so a folder
    # that fails more than one gets the message the command line would give.
    if resume and force:
        raise UsageError("--resume and --force cannot be used together")
    out = out_dir.absolute()
    try:
        found = _results_folder(out, None if input_dir is None else input_dir.absolute())
        if found is ResultsFolder.EARLIER_RUN and _force_pending(out):
            found = ResultsFolder.FORCE_PENDING
    except (OSError, RuntimeError) as exc:  # RuntimeError: a symlink loop on Python 3.11
        raise UsageError(_cannot_read_message(out, exc)) from exc
    if resume and found is ResultsFolder.FORCE_PENDING:
        raise UsageError(_force_pending_message(out))
    _check_done_markers(out, names)
    if out.is_dir() and _in_use(out):
        raise ResultsFolderInUse(_in_use_message(out), found)
    return found


def done_proxies(out_dir: Path, names: Sequence[str]) -> list[str]:
    """The proxies among ``names`` that --resume would skip in ``out_dir``: those with a ``.done`` marker.

    Read-only; a name that is not safe, or whose marker cannot be checked, counts as not done.
    """
    out = out_dir.absolute()
    done: list[str] = []
    for name in names:
        if unsafe_name_reason(name) is not None:
            continue
        try:
            if _done_folder(out, name) is not None:
                done.append(name)
        except (OSError, UnsafePathError):
            continue
    return done


def _done_folder(out: Path, name: str) -> Path | None:
    """The folder of proxy ``name`` in ``out`` that holds its ``.done`` marker, or None."""
    return next(
        (folder for folder in _proxy_dirs(out, name) if safefs.is_regular_file(out, folder / layout.DONE_MARKER_NAME)),
        None,
    )


def _in_use(out: Path) -> bool:
    """Whether another a2m run holds the results folder's lock; never creates the lock file."""
    try:
        return safefs.lock_held(out, layout.lock_path(out))
    except (OSError, UnsafePathError):
        return False


def _in_use_message(out: Path) -> str:
    return f"results folder {out} is in use by another a2m run; wait for it to finish, then run again"


def _force_pending_message(out: Path) -> str:
    return (
        f"results folder {out} has a --force run that stopped before it removed every earlier .done "
        "marker, so --resume cannot tell finished proxies from old ones; rerun with --force to redo every proxy"
    )


def _cannot_read_message(out: Path, exc: OSError | RuntimeError) -> str:
    text = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
    return f"cannot read results folder {out}: {text}"


def _absolute_paths(options: RunOptions) -> RunOptions:
    """``options`` with EXPORTS, --out and --golden made absolute (Path.absolute: links are not resolved)."""
    golden = options.golden.absolute() if options.golden is not None else None
    return dataclasses.replace(
        options, input_dir=options.input_dir.absolute(), out_dir=options.out_dir.absolute(), golden=golden
    )


def _check_golden(options: RunOptions) -> None:
    """UsageError unless --golden (when given) names an existing folder."""
    golden = options.golden
    if golden is None:
        return
    if not golden.exists():
        raise UsageError(f"golden recordings folder {golden} does not exist")
    if not golden.is_dir():
        raise UsageError(f"golden recordings path {golden} is not a folder")


def _make_provider(options: RunOptions) -> Provider:
    """The AI provider --llm picks, and its prompt files, checked before anything is processed: a missing API key,
    SDK or prompt file stops the run with one clear line. The fix prompt is checked when the AI fix loop may run
    (--max-fix-attempts above 0, and the apps may run: --mock-backends or --golden without --no-runtime)."""
    try:
        load_prompts()
        if _fix_loop_may_run(options):
            load_prompt(FIX_PROMPT_FILE, what="fix")
        return make_provider(options.llm.value)
    except (ProviderSetupError, PromptError) as exc:
        raise UsageError(str(exc)) from None


def _fix_loop_may_run(options: RunOptions) -> bool:
    return options.max_fix_attempts > 0 and not options.no_runtime and (options.mock_backends or options.golden is not None)


def _check_not_a_bundle(input_dir: Path) -> None:
    """UsageError when EXPORTS is itself one bundle (a real apiproxy/ or sharedflowbundle/ folder at its top).

    Uses lstat, so a link named apiproxy is not followed and does not count.
    """
    for root in BUNDLE_ROOTS:
        try:
            info = os.lstat(input_dir / root)
        except (OSError, ValueError):
            continue
        if stat.S_ISDIR(info.st_mode) and not safefs.is_link_like(info):
            parent = Path(os.path.abspath(input_dir)).parent
            raise UsageError(
                f"{input_dir} is itself a bundle (it has {root}/ at its top); EXPORTS must be the folder "
                f"that holds bundles, so pass the folder that contains it, for example {parent}"
            )


def _check_out_dir(options: RunOptions) -> None:
    try:
        _check_out_dir_unguarded(options)
    except (OSError, RuntimeError) as exc:  # RuntimeError: a symlink loop on Python 3.11
        raise UsageError(_cannot_read_message(options.out_dir, exc)) from exc


def _check_out_dir_unguarded(options: RunOptions) -> None:
    out = options.out_dir
    if _results_folder(out, options.input_dir) is ResultsFolder.NEW:
        return
    if not (options.resume or options.force):
        raise UsageError(
            f"results folder {out} already has output from an earlier run; "
            "use --resume to continue it or --force to redo every proxy"
        )
    if options.resume and _force_pending(out):
        raise UsageError(_force_pending_message(out))


def _results_folder(out: Path, input_dir: Path | None) -> ResultsFolder:
    """NEW or EARLIER_RUN for a usable results folder ``out``; UsageError for one no run may use.

    ``input_dir`` None skips the check against the input folder. Never tells EARLIER_RUN from
    FORCE_PENDING (see :func:`_force_pending`). May raise OSError or RuntimeError from reading.
    """
    if input_dir is not None and (
        pathid.is_same_or_inside(out, input_dir) or pathid.is_same_or_inside(input_dir, out)
    ):
        raise UsageError(
            f"results folder {out} must not be the input folder {input_dir}, be inside it, or contain it"
        )
    if not out.exists():
        _check_out_creatable(out)
        return ResultsFolder.NEW
    if not out.is_dir():
        raise UsageError(f"results path {out} exists and is not a folder")
    if not any(out.iterdir()):
        return ResultsFolder.NEW
    _check_owned_entries(out)
    # The lock file alone does not make a folder used: a run may have stopped
    # between taking the lock and writing anything else.
    if not any(entry.name != layout.LOCK_NAME for entry in out.iterdir()):
        return ResultsFolder.NEW
    if not safefs.is_regular_file(out, layout.results_marker_path(out)):
        raise UsageError(
            f"results folder {out} is not empty and has no {layout.RESULTS_MARKER_NAME} marker, "
            "so it is not an a2m results folder; choose a new or empty folder"
        )
    return ResultsFolder.EARLIER_RUN


def _force_pending(out: Path) -> bool:
    """Whether an earlier --force run in ``out`` stopped before it removed every earlier ``.done`` marker."""
    return safefs.is_regular_file(out, layout.force_pending_path(out))


def _check_out_creatable(out: Path) -> None:
    """UsageError unless the missing results folder ``out`` can be made where it is.

    ``out.exists()`` follows links, so a dangling link at ``out`` (a link to a
    disk that is not mounted) looks missing; so does a path under a file. Both
    would only fail later, with a misleading "File exists", once the run
    starts. The nearest part of the path that is there must be a folder.
    """
    if safefs.is_link(out):
        raise UsageError(
            f"results path {out} is a symbolic link to {os.readlink(out)}, which does not exist; "
            "create that folder or choose another --out"
        )
    for parent in out.parents:
        if not os.path.lexists(parent):
            continue
        if parent.is_dir():
            return
        if safefs.is_link(parent):
            raise UsageError(
                f"results path {out} cannot be created: {parent} is a symbolic link to {os.readlink(parent)}, "
                "which is not a folder; choose another --out"
            )
        raise UsageError(f"results path {out} cannot be created: {parent} is not a folder; choose another --out")


def _check_owned_entries(out: Path) -> None:
    """Refuse a results folder where a2m's own entries are links or the wrong kind of file.

    a2m writes run.log, its marker and its scratch folder directly; a link at
    any of them could redirect those writes and deletes outside the results
    folder, and a FIFO, socket or device at a file entry could block a2m
    forever when it opens it. Each entry must be absent or exactly its kind.
    """
    expected = {
        layout.RUN_LOG_NAME: "a plain file",
        layout.RESULTS_MARKER_NAME: "a plain file",
        layout.WORK_DIR_NAME: "a folder",
        layout.LOCK_NAME: "a plain file",
        layout.FORCE_PENDING_NAME: "a plain file",
        layout.SUMMARY_MD_NAME: "a plain file",
        layout.SUMMARY_JSON_NAME: "a plain file",
        **{bucket: "a folder" for bucket in layout.BUCKET_DIR_NAMES},
    }
    for name, kind in expected.items():
        _check_entry_kind(out, out / name, name, folder=kind == "a folder")


def _check_entry_kind(out: Path, path: Path, shown: str, *, folder: bool) -> None:
    """UsageError unless ``path`` is absent, or (not following a link) a plain file or a folder as asked."""
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return
    mode = info.st_mode
    if safefs.is_link_like(info):
        raise UsageError(
            f"results folder {out} has a symbolic link at {shown}; a2m will not write through it, "
            "remove the link and run again"
        )
    if not (stat.S_ISDIR(mode) if folder else stat.S_ISREG(mode)):
        kind = "a folder" if folder else "a plain file"
        raise UsageError(f"results folder {out} has {shown}, which is not {kind}; remove it and run again")


def _check_done_markers(out: Path, names: Sequence[str]) -> None:
    """Refuse a results folder where a proxy's ``.done`` marker exists but is not a plain file.

    --resume trusts the marker, so it must be a plain file or absent. A proxy
    folder that is itself a link or not a folder is left to the guarded delete,
    which removes it as an entry without following it. A marker that cannot be
    read at all (permissions) is left to that proxy alone: it fails on its own
    when the run gets to it, and the batch goes on.
    """
    for name in names:
        if unsafe_name_reason(name) is not None:
            continue
        for proxy_dir in _proxy_dirs(out, name):
            try:
                info = os.lstat(proxy_dir)
                if not stat.S_ISDIR(info.st_mode) or safefs.is_link_like(info):
                    continue
                marker = proxy_dir / layout.DONE_MARKER_NAME
                shown = proxy_dir.relative_to(out).as_posix()
                _check_entry_kind(out, marker, f"{shown}/{layout.DONE_MARKER_NAME}", folder=False)
            except OSError:
                continue


def _proxy_dirs(out: Path, name: str, *, flat: bool = True, buckets: bool = True) -> list[Path]:
    """Every folder proxy ``name`` may have in ``out``: ``<out>/<name>`` (custom stage lists, and the working
    folder of a reporting run) and ``<out>/<bucket>/<name>``. ``name`` must be a safe name."""
    found = [layout.proxy_out_dir(out, name)] if flat else []
    if buckets:
        found += [layout.bucket_proxy_dir(out, bucket, name) for bucket in layout.BUCKET_DIR_NAMES]
    return found


def run_batch(
    plan: RunPlan, stages: Sequence[Stage] | None = None, progress: ProgressCallback | None = None
) -> BatchResult:
    """Create the results folder, log discovery, and process every selected proxy.

    ``stages`` defaults to :func:`default_stages`. A stage with a ``close()``
    method (the verification stage, which may have started a Mule runtime) is
    closed when the batch ends, however it ends. ``progress`` receives the
    batch's progress events (see :mod:`a2m.progress`); it never changes the run.

    The whole run holds an exclusive lock on the results folder, so a second
    run on the same folder stops with :class:`UsageError` instead of deleting
    or overwriting this run's output. The checks of :func:`prepare_run` are
    repeated once the lock is held, since another run may have changed the
    folder in between.
    """
    options = plan.options
    out = options.out_dir
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UsageError(f"cannot create results folder {out}: {exc.strerror or exc}") from exc
    _check_out_dir(options)  # before opening the lock file: it must not be a FIFO or a link
    try:
        with _stop_on_termination(), safefs.exclusive_lock(out, layout.lock_path(out)):
            _check_out_dir(options)
            _check_done_markers(out, plan.discovery.candidate_names())
            reporter = Progress(progress, len(plan.selected))
            return _run_locked(plan, default_stages() if stages is None else stages, reporter)
    except LockHeldError:
        raise UsageError(_in_use_message(out)) from None
    except NotPlainFileError as exc:
        raise UsageError(f"results folder {out}: {exc}; remove it and run again") from exc


def _run_locked(plan: RunPlan, stages: Sequence[Stage], progress: Progress) -> BatchResult:
    options = plan.options
    out = options.out_dir
    marker = layout.results_marker_path(out)
    if not safefs.is_regular_file(out, marker):
        safefs.write_text_atomic(out, marker, layout.RESULTS_MARKER_TEXT)
    result = BatchResult(log_path=layout.run_log_path(out))

    log_path = layout.run_log_path(out)

    def open_log() -> int:
        return safefs.open_plain_file(out, log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)

    with run_log(log_path, open_log) as log:
        log.info("run started: input %s, results %s", options.input_dir, out)
        log.info(
            "options: only=%s resume=%s force=%s llm=%s runtime=%s golden=%s mock_backends=%s "
            "max_fix_attempts=%d",
            options.only or "-",
            options.resume,
            options.force,
            options.llm.value,
            "off" if options.no_runtime else "auto",
            options.golden or "-",
            options.mock_backends,
            options.max_fix_attempts,
        )
        reporter = _reporter(stages)
        try:
            unlisted = _run_proxies(plan, stages, result, log, progress)
            if reporter is not None:
                reporter.finish_batch(out, unlisted, _roots(options))
        finally:
            _close_stages(stages)
            result.notices.extend(_stage_notices(stages))
        _remove_empty_work_root(out)
        log.info(
            "run finished: %d done, %d skipped as already done, %d refused, %d failed",
            len(result.finished),
            len(result.skipped),
            len(result.refused),
            len(result.crashed),
        )
        # inside the run.log block, so a progress callback that fails on this last event is logged there too
        progress.run_finished(
            finished=len(result.finished), skipped=len(result.skipped), refused=len(result.refused),
            failed=len(result.crashed),
        )
    return result


def _close_stages(stages: Sequence[Stage]) -> None:
    """Close every stage that has ``close()`` (e.g. stop the batch's Mule runtime); a failure is logged."""
    log = get_logger()
    for stage in stages:
        close = getattr(stage, "close", None)
        if close is None:
            continue
        try:
            close()
        except Exception as exc:  # batch boundary: a stage that cannot clean up must not hide the run's result
            log.exception("could not close stage %s: %s: %s", _stage_name(stage), type(exc).__name__, exc)  # noqa: TRY401


def _stage_notices(stages: Sequence[Stage]) -> list[str]:
    """The batch-wide notices of every stage that has them (e.g. verification skipped), each once."""
    found: list[str] = []
    for stage in stages:
        for notice in getattr(stage, "notices", ()):
            if isinstance(notice, str) and notice not in found:
                found.append(notice)
    return found


def _roots(options: RunOptions) -> list[tuple[Path, str]]:
    """The absolute folders a report shows by label instead (the results, input and --golden folders)."""
    roots = [(options.out_dir, "<results>"), (options.input_dir, "<exports>")]
    if options.golden is not None:
        roots.append((options.golden, "<golden>"))
    return roots


def _run_proxies(
    plan: RunPlan, stages: Sequence[Stage], result: BatchResult, log: logging.Logger, progress: Progress
) -> list[tuple[str, str]]:
    """Process every selected proxy; return the refused items a reporting run could give no folder (name, reason)."""
    options = plan.options
    out = options.out_dir
    current: str | None = None
    cleared = not options.force
    reporter = _reporter(stages)
    unlisted: list[tuple[str, str]] = []
    try:
        _log_discovery(plan)
        not_removed = _remove_stale_results_of_refused(plan)
        not_cleared = _clear_done_markers_for_force(plan) if options.force else set()
        cleared = not not_cleared
        shared_flows = _read_shared_flows(plan) if plan.selected else ()
        progress.run_started()

        for index, source in enumerate(plan.selected, 1):
            current = source.name
            with progress.proxy(index, source.name):
                outcome, bucket = _run_one(plan, stages, result, log, progress, source, not_cleared, shared_flows)
                progress.finished(outcome, bucket)

        current = None
        selected_names = {source.name for source in plan.selected}
        result.crashed.extend(sorted(not_cleared - selected_names))
        processed = {collision_key(name) for name in selected_names}
        for item in plan.selected_rejected:
            (result.crashed if item.name in not_removed else result.refused).append(item.name)
            if reporter is None or item.name in not_removed:
                continue
            if unsafe_name_reason(item.name) is not None or collision_key(item.name) in processed:
                unlisted.append((item.name, item.reason))
                continue
            try:
                reporter.write_unsupported(
                    out, item.name, item.path.name, item.reason, refused=True, roots=_roots(options)
                )
            except (OSError, UnsafePathError) as exc:
                log.error("could not write the report of refused %s: %s", item.name, exc)
                unlisted.append((item.name, item.reason))
        return unlisted
    except KeyboardInterrupt as exc:
        where = f" while processing {current}" if current is not None else ""
        advice = _advice(options, cleared=cleared)
        if cleared:
            log.error("run interrupted%s; finished proxies keep their .done marker, %s", where, advice)
        else:
            log.error("run interrupted%s before earlier .done markers were cleared; %s", where, advice)
        raise RunInterrupted(advice) from exc


def _run_one(
    plan: RunPlan,
    stages: Sequence[Stage],
    result: BatchResult,
    log: logging.Logger,
    progress: Progress,
    source: BundleSource,
    not_cleared: set[str],
    shared_flows: tuple[Bundle, ...],
) -> tuple[Outcome, str | None]:
    """Skip or process one selected proxy; return how it ended and the bucket it is in (None: not known)."""
    options = plan.options
    out = options.out_dir
    if source.name in not_cleared:
        result.crashed.append(source.name)
        return Outcome.FAILED, None
    try:
        done_in = _done_folder(out, source.name) if options.resume else None
    except (OSError, UnsafePathError) as exc:
        log.error("failed %s: cannot check its earlier result: %s", source.name, exc)
        result.crashed.append(source.name)
        return Outcome.FAILED, None
    if done_in is not None:
        log.info("skipped %s: already done (resume)", source.name)
        result.skipped.append(source.name)
        # <out>/<bucket>/<name>, or <out>/<name> (custom stage lists): no bucket.
        return Outcome.SKIPPED, done_in.parent.name if done_in.parent != out else None
    progress.started()
    return _process(source, options, stages, result, shared_flows, plan.provider)


def _log_discovery(plan: RunPlan) -> None:
    log = get_logger()
    discovery = plan.discovery
    for source in discovery.proxies:
        log.info("found proxy %s (%s %s%s)", source.name, source.kind.value, source.path.name, _wrapper_note(source))
    for source in discovery.shared_flows:
        log.info(
            "found shared flow bundle %s (%s %s%s); it is not migrated as a proxy of its own",
            source.name,
            source.kind.value,
            source.path.name,
            _wrapper_note(source),
        )
    for item in discovery.skipped:
        log.info("skipped %s: %s", item.path.name, item.reason)
    selected_rejected = set(plan.selected_rejected)
    for rejected in discovery.rejected:
        if rejected in selected_rejected:
            log.error("refused %s (%s): %s", rejected.name, rejected.path.name, rejected.reason)
        else:
            log.info("skipped %s: not selected by --only", rejected.name)
    if plan.options.only is not None:
        log.info("only processing %s (--only)", plan.options.only)


def _wrapper_note(source: BundleSource) -> str:
    if source.wrapper is None:
        return ""
    return f", bundle root found inside its top folder {source.wrapper}/"


def _remove_stale_results_of_refused(plan: RunPlan) -> set[str]:
    """Delete earlier output of every proxy refused in this run; return the names that could not be removed.

    A refused proxy's input changed since its output was made, so an old
    ``.done`` must not survive: --resume would otherwise skip the new bundle.
    Names that collide (see :func:`a2m.layout.collision_key`) with a proxy processed in this run are
    left alone; that proxy's own folder is redone anyway. The caller records
    the returned names as failed instead of refused (see :class:`BatchResult`).
    """
    log = get_logger()
    out = plan.options.out_dir
    processed = {collision_key(source.name) for source in plan.selected}
    not_removed: set[str] = set()
    for name in sorted({item.name for item in plan.selected_rejected}):
        if unsafe_name_reason(name) is not None or collision_key(name) in processed:
            continue
        try:
            removed = False
            for folder in _proxy_dirs(out, name):
                removed = safefs.remove(out, folder) or removed
        except (OSError, UnsafePathError) as exc:
            log.error(
                "failed %s: could not remove its earlier results, which no longer match its input: %s", name, exc
            )
            not_removed.add(name)
            continue
        if removed:
            log.info("removed earlier results of %s: it was refused in this run", name)
    return not_removed


def _clear_done_markers_for_force(plan: RunPlan) -> set[str]:
    """--force: remove the ``.done`` marker of every selected proxy before any is redone; return the names that failed.

    --force redoes each proxy only when its turn comes. If the run stops before
    then (Ctrl-C, a kill), an earlier marker would let a later --resume skip
    that proxy and keep its old output. With the markers gone first, --resume
    after an interrupted --force redoes exactly what the forced run did not
    finish. Only markers --resume would trust (plain files, reached without a
    link) are removed.

    The removal is bracketed by the run-level force-pending marker, written
    atomically before the first marker is removed and removed only after the
    last one is gone, so a run killed in between leaves it behind and --resume
    is refused (see :func:`_check_out_dir_unguarded`). A force-pending marker
    left by an earlier run widens this run's removal to every proxy folder in
    the results folder, since that run's selection is not known. A marker
    that cannot be removed leaves the force-pending marker in place too; the
    caller records the returned names as failed, not processed.
    """
    log = get_logger()
    out = plan.options.out_dir
    pending = layout.force_pending_path(out)
    names = [source.name for source in plan.selected]
    if safefs.is_regular_file(out, pending):
        log.info("an earlier --force run stopped before it removed every .done marker; removing all of them")
        names += sorted(_proxy_folder_names(out) - set(names))
    safefs.write_text_atomic(out, pending, layout.FORCE_PENDING_TEXT)
    not_cleared: set[str] = set()
    cleared = 0
    for name in names:
        try:
            for folder in _proxy_dirs(out, name):
                marker = folder / layout.DONE_MARKER_NAME
                if safefs.is_regular_file(out, marker):
                    safefs.remove(out, marker)
                    cleared += 1
        except (OSError, UnsafePathError) as exc:
            log.error("failed %s: could not remove its earlier .done marker before redoing it (--force): %s", name, exc)
            not_cleared.add(name)
    if cleared:
        log.info("--force: removed the .done marker of %d earlier finished proxies before redoing any", cleared)
    if not not_cleared:
        safefs.remove(out, pending)
    return not_cleared


def _proxy_folder_names(out: Path) -> set[str]:
    """Names of the entries directly in ``out`` or in one of its bucket folders that could be a proxy's folder."""
    names = {entry.name for entry in out.iterdir() if unsafe_name_reason(entry.name) is None}
    for bucket in layout.BUCKET_DIR_NAMES:
        folder = layout.bucket_dir(out, bucket)
        if safefs.is_link(folder) or not folder.is_dir():
            continue
        names |= {entry.name for entry in folder.iterdir() if unsafe_name_reason(entry.name) is None}
    return names


def _read_shared_flows(plan: RunPlan) -> tuple[Bundle, ...]:
    """Read every shared flow bundle of the input folder, each through its own sanitized working copy.

    A shared flow bundle that cannot be copied or read is logged and left out;
    FlowCallout steps that call it are then reported as not generated. The
    working copies are removed again before any proxy runs.
    """
    log = get_logger()
    out = plan.options.out_dir
    found: list[Bundle] = []
    for source in plan.discovery.shared_flows:
        work_dir: Path | None = None
        try:
            work_dir = layout.shared_flow_work_dir(out, source.name)
            safefs.remove(out, work_dir)
            safefs.make_dirs(out, work_dir)
            _materialize(source, work_dir)
            bundle_dir = work_dir if source.wrapper is None else work_dir / source.wrapper
            bundle = read_bundle(bundle_dir, label=source.name)
            log.info("read shared flow bundle %s (%d shared flows, %d policies)", bundle.name,
                     len(bundle.shared_flows), len(bundle.policies))
            found.append(bundle)
        except (BundleError, OSError, UnsafePathError) as exc:
            log.error(
                "could not read shared flow bundle %s (%s): %s; steps that call it are not generated",
                source.name,
                source.path.name,
                exc,
            )
        finally:
            try:
                if work_dir is not None:
                    safefs.remove(out, work_dir)
            except (OSError, UnsafePathError) as exc:
                log.warning("could not remove unpacked copy of %s at %s: %s", source.name, work_dir, exc)
    try:
        safefs.remove_empty_dir(out, layout.shared_flows_work_root(out))
    except (OSError, UnsafePathError) as exc:
        log.warning("could not remove the shared flow work folder: %s", exc)
    return tuple(found)


def _process(
    source: BundleSource,
    options: RunOptions,
    stages: Sequence[Stage],
    result: BatchResult,
    shared_flows: tuple[Bundle, ...] = (),
    provider: Provider | None = None,
) -> tuple[Outcome, str | None]:
    """Run ``source`` through the stages; return how it ended and the bucket it is in (None: not known)."""
    log = get_logger()
    out = options.out_dir
    bucket: str | None = None
    work_dir: Path | None = None
    bundle_dir: Path | None = None
    reporter = _reporter(stages)
    log.info("processing %s", source.name)
    current = "prepare"
    try:
        proxy_dir = layout.proxy_out_dir(out, source.name)
        work_dir = layout.proxy_work_dir(out, source.name)
        # First, so a refused bundle leaves no stale .done, and the proxy never has two folders.
        for folder in _proxy_dirs(out, source.name):
            safefs.remove(out, folder)
        safefs.remove(out, work_dir)
        safefs.make_dirs(out, work_dir)
        _materialize(source, work_dir)
        bundle_dir = work_dir if source.wrapper is None else work_dir / source.wrapper
        safefs.make_dirs(out, proxy_dir)
        context = ProxyContext(
            name=source.name,
            source_name=source.path.name,
            kind=source.kind,
            bundle_dir=bundle_dir,
            out_dir=proxy_dir,
            options=StageOptions.of(options, provider),
            shared_flows=shared_flows,
        )
        for stage in stages:
            current = _stage_name(stage)
            stage(context)
            log.info("%s: stage %s finished", source.name, current)
        current = "finish"
        if reporter is None:
            _write_done_marker(out, source.name)
        else:
            bucket = _place(out, source.name, proxy_dir, reporter)
    except BundleError as exc:
        log.error("refused %s (%s): %s", source.name, source.path.name, exc)
        result.refused.append(source.name)
        if reporter is not None:
            bucket = _report_unsupported(reporter, options, source, str(exc), refused=True, bundle_dir=bundle_dir)
        return Outcome.REFUSED, bucket
    except Exception as exc:  # proxy boundary: one proxy failing never stops the batch
        # The entry names the error on its own line too, so grepping run.log finds it without the traceback.
        log.exception(
            "failed %s in stage %s: %s: %s",
            source.name,
            current,
            type(exc).__name__,
            exc,  # noqa: TRY401
        )
        result.crashed.append(source.name)
        if reporter is not None:
            cause = f"stage {current} failed: {type(exc).__name__}: {exc}"
            bucket = _report_unsupported(reporter, options, source, cause, refused=False, bundle_dir=bundle_dir)
        return Outcome.FAILED, bucket
    else:
        log.info("done %s", source.name)
        result.finished.append(source.name)
        return Outcome.FINISHED, bucket
    finally:
        try:
            if work_dir is not None:
                safefs.remove(out, work_dir)
        except (OSError, UnsafePathError) as exc:
            log.warning("could not remove unpacked copy of %s at %s: %s", source.name, work_dir, exc)


def _place(out: Path, name: str, proxy_dir: Path, reporter: Reporter) -> str:
    """Move the finished proxy's folder into the bucket the reporter chose, then write its ``.done`` there last;
    return the bucket."""
    bucket = reporter.bucket_for(name)
    if bucket not in layout.BUCKET_DIR_NAMES:
        raise RuntimeError(f"the report stage chose no bucket for {name}")
    final = layout.bucket_proxy_dir(out, bucket, name)
    safefs.make_dirs(out, final.parent)
    safefs.remove(out, final)
    safefs.move(out, proxy_dir, final)
    safefs.write_text_atomic(out, final / layout.DONE_MARKER_NAME, f"a2m finished {name}\n")
    get_logger().info("%s: results in %s/%s/", name, bucket, name)
    return bucket


def _report_unsupported(
    reporter: Reporter,
    options: RunOptions,
    source: BundleSource,
    cause: str,
    *,
    refused: bool,
    bundle_dir: Path | None,
) -> str | None:
    """Replace whatever the proxy has in the results folder with ``unsupported/<proxy>/REPORT.md`` naming
    ``cause``; a failure to do so is logged, never raised (the proxy is already counted as refused or failed).
    Return the bucket it is in, or None when the report could not be written."""
    out = options.out_dir
    log = get_logger()
    try:
        for folder in _proxy_dirs(out, source.name):
            safefs.remove(out, folder)
        reporter.write_unsupported(
            out, source.name, source.path.name, cause, refused=refused, bundle_dir=bundle_dir, roots=_roots(options)
        )
        log.info("%s: results in %s/%s/", source.name, layout.UNSUPPORTED_DIR_NAME, source.name)
    except (OSError, UnsafePathError) as exc:
        log.error("could not write the unsupported report of %s: %s", source.name, exc)
        return None
    return layout.UNSUPPORTED_DIR_NAME


def _materialize(source: BundleSource, work_dir: Path) -> None:
    """Copy the bundle root of ``source`` into the empty ``work_dir``: the only copy any stage sees.

    Folders and zips alike: only the bundle root (with its wrapper), only plain
    files and real folders, within the same limits, never through a link;
    entries beside it are noted in run.log. Anything else raises
    :class:`BundleError` (refused). The copy is then classified again with
    :func:`bundle_root` and refused unless it holds exactly the bundle root
    discovery found, so ``bundle_dir`` always holds a verified copy.
    """
    if source.kind is SourceKind.ZIP:
        left_out = extract_zip(source.path, work_dir)
    else:
        left_out = copy_folder_bundle(source.path, work_dir)
    expected = BundleRoot(source.root, source.wrapper)
    found = bundle_root(folder_subfolders(work_dir))
    if found != expected:
        raise UnsafeBundleError(
            f"{source.path.name} changed after it was found: expected {_layout_text(expected)} in it, "
            f"found {_layout_text(found) if found is not None else 'no bundle'}"
        )
    if left_out:
        shown = ", ".join(left_out[:MAX_NAMES_IN_MESSAGE]) + (", ..." if len(left_out) > MAX_NAMES_IN_MESSAGE else "")
        get_logger().info("%s: only %s is used; left out beside it: %s", source.name, _layout_text(expected), shown)


def _layout_text(found: BundleRoot) -> str:
    return f"{found.wrapper + '/' if found.wrapper else ''}{found.root}/"


def _stage_name(stage: Stage) -> str:
    return getattr(stage, "__name__", None) or type(stage).__name__


def _write_done_marker(out: Path, name: str) -> None:
    """Write the marker last, atomically: a temp file renamed into place."""
    safefs.write_text_atomic(out, layout.done_marker_path(out, name), f"a2m finished {name}\n")


def _remove_empty_work_root(out: Path) -> None:
    safefs.remove_empty_dir(out, layout.work_root(out))
