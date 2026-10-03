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
"""

from __future__ import annotations

import os
import shlex
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from a2m import layout, pathid, safefs
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
from a2m.generator import generate_project
from a2m.ir import Bundle
from a2m.layout import collision_key, unsafe_name_reason
from a2m.parser import read_bundle
from a2m.runlog import get_logger, run_log

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


@dataclass(frozen=True, slots=True)
class StageOptions:
    """The run options a per-proxy stage may use; no input or results paths."""

    golden: Path | None = None
    mock_backends: bool = False
    max_fix_attempts: int = 3
    llm: LlmChoice = LlmChoice.CLAUDE
    no_runtime: bool = False

    @classmethod
    def of(cls, options: RunOptions) -> StageOptions:
        return cls(
            golden=options.golden,
            mock_backends=options.mock_backends,
            max_fix_attempts=options.max_fix_attempts,
            llm=options.llm,
            no_runtime=options.no_runtime,
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
    result = generate_project(bundle, dest, shared_flows=context.shared_flows, results_root=context.out_dir)
    log.info("%s: wrote Mule project %s/ (%d files)", context.name, layout.MULE_APP_DIR_NAME, len(result.files))
    for item in result.unsupported:
        log.warning("%s: not generated: %s: %s", context.name, item.name, item.reason)
    for pending in result.pending:
        log.info(
            "%s: %s %s (%s) keeps its condition for translation: %s",
            context.name,
            pending.kind,
            pending.name,
            pending.endpoint,
            pending.condition,
        )


# The per-proxy pipeline. Later checkpoints add verification stages here;
# tests replace it through ``stages=``.
DEFAULT_STAGES: tuple[Stage, ...] = (parse, generate)


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


def prepare_run(options: RunOptions) -> RunPlan:
    """Check everything that can be checked before writing; raise UsageError on problems."""
    if options.resume and options.force:
        raise UsageError("--resume and --force cannot be used together")
    if options.max_fix_attempts < 0:
        raise UsageError("--max-fix-attempts must be 0 or greater")
    discovery = discover(options.input_dir)
    names = discovery.candidate_names()
    if not names:
        _check_not_a_bundle(options.input_dir)
    _check_out_dir(options)

    _check_done_markers(options.out_dir, names)
    if not names:
        shared = f" ({len(discovery.shared_flows)} shared flow bundles only)" if discovery.shared_flows else ""
        raise UsageError(
            f"no proxies found in {options.input_dir}{shared}; expected folders or .zip files "
            f"with an {PROXY_ROOT}/ folder at the top, or inside one top folder"
        )
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
    )


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
        text = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        raise UsageError(f"cannot read results folder {options.out_dir}: {text}") from exc


def _check_out_dir_unguarded(options: RunOptions) -> None:
    out = options.out_dir
    if pathid.is_same_or_inside(out, options.input_dir) or pathid.is_same_or_inside(options.input_dir, out):
        raise UsageError(
            f"results folder {out} must not be the input folder {options.input_dir}, be inside it, or contain it"
        )
    if not out.exists():
        _check_out_creatable(out)
        return
    if not out.is_dir():
        raise UsageError(f"results path {out} exists and is not a folder")
    if not any(out.iterdir()):
        return
    _check_owned_entries(out)
    # The lock file alone does not make a folder used: a run may have stopped
    # between taking the lock and writing anything else.
    if not any(entry.name != layout.LOCK_NAME for entry in out.iterdir()):
        return
    if not safefs.is_regular_file(out, layout.results_marker_path(out)):
        raise UsageError(
            f"results folder {out} is not empty and has no {layout.RESULTS_MARKER_NAME} marker, "
            "so it is not an a2m results folder; choose a new or empty folder"
        )
    if not (options.resume or options.force):
        raise UsageError(
            f"results folder {out} already has output from an earlier run; "
            "use --resume to continue it or --force to redo every proxy"
        )
    if options.resume and safefs.is_regular_file(out, layout.force_pending_path(out)):
        raise UsageError(
            f"results folder {out} has a --force run that stopped before it removed every earlier .done "
            "marker, so --resume cannot tell finished proxies from old ones; rerun with --force to redo every proxy"
        )


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
        proxy_dir = out / name
        try:
            info = os.lstat(proxy_dir)
            if not stat.S_ISDIR(info.st_mode) or safefs.is_link_like(info):
                continue
            marker = proxy_dir / layout.DONE_MARKER_NAME
            _check_entry_kind(out, marker, f"{name}/{layout.DONE_MARKER_NAME}", folder=False)
        except OSError:
            continue


def run_batch(plan: RunPlan, stages: Sequence[Stage] = DEFAULT_STAGES) -> BatchResult:
    """Create the results folder, log discovery, and process every selected proxy.

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
        with safefs.exclusive_lock(out, layout.lock_path(out)):
            _check_out_dir(options)
            _check_done_markers(out, plan.discovery.candidate_names())
            return _run_locked(plan, stages)
    except LockHeldError:
        raise UsageError(
            f"results folder {out} is in use by another a2m run; wait for it to finish, then run again"
        ) from None
    except NotPlainFileError as exc:
        raise UsageError(f"results folder {out}: {exc}; remove it and run again") from exc


def _run_locked(plan: RunPlan, stages: Sequence[Stage]) -> BatchResult:
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
        current: str | None = None
        cleared = not options.force
        try:
            _log_discovery(plan)
            not_removed = _remove_stale_results_of_refused(plan)
            not_cleared = _clear_done_markers_for_force(plan) if options.force else set()
            cleared = not not_cleared
            shared_flows = _read_shared_flows(plan) if plan.selected else ()

            for source in plan.selected:
                current = source.name
                if source.name in not_cleared:
                    result.crashed.append(source.name)
                    continue
                try:
                    already_done = options.resume and safefs.is_regular_file(
                        out, layout.done_marker_path(out, source.name)
                    )
                except (OSError, UnsafePathError) as exc:
                    log.error("failed %s: cannot check its earlier result: %s", source.name, exc)
                    result.crashed.append(source.name)
                    continue
                if already_done:
                    log.info("skipped %s: already done (resume)", source.name)
                    result.skipped.append(source.name)
                    continue
                _process(source, options, stages, result, shared_flows)

            current = None
            selected_names = {source.name for source in plan.selected}
            result.crashed.extend(sorted(not_cleared - selected_names))
            for item in plan.selected_rejected:
                (result.crashed if item.name in not_removed else result.refused).append(item.name)
        except KeyboardInterrupt as exc:
            where = f" while processing {current}" if current is not None else ""
            advice = _advice(options, cleared=cleared)
            if cleared:
                log.error("run interrupted%s; finished proxies keep their .done marker, %s", where, advice)
            else:
                log.error("run interrupted%s before earlier .done markers were cleared; %s", where, advice)
            raise RunInterrupted(advice) from exc

        _remove_empty_work_root(out)
        log.info(
            "run finished: %d done, %d skipped as already done, %d refused, %d failed",
            len(result.finished),
            len(result.skipped),
            len(result.refused),
            len(result.crashed),
        )
    return result


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
            removed = safefs.remove(out, layout.proxy_out_dir(out, name))
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
            marker = layout.done_marker_path(out, name)
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
    """Names of the entries directly in ``out`` that could be a proxy's folder."""
    return {entry.name for entry in out.iterdir() if unsafe_name_reason(entry.name) is None}


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
) -> None:
    log = get_logger()
    out = options.out_dir
    work_dir: Path | None = None
    log.info("processing %s", source.name)
    current = "prepare"
    try:
        proxy_dir = layout.proxy_out_dir(out, source.name)
        work_dir = layout.proxy_work_dir(out, source.name)
        safefs.remove(out, proxy_dir)  # first, so a refused bundle leaves no stale .done
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
            options=StageOptions.of(options),
            shared_flows=shared_flows,
        )
        for stage in stages:
            current = _stage_name(stage)
            stage(context)
            log.info("%s: stage %s finished", source.name, current)
        current = "finish"
        _write_done_marker(out, source.name)
    except BundleError as exc:
        log.error("refused %s (%s): %s", source.name, source.path.name, exc)
        result.refused.append(source.name)
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
    else:
        log.info("done %s", source.name)
        result.finished.append(source.name)
    finally:
        try:
            if work_dir is not None:
                safefs.remove(out, work_dir)
        except (OSError, UnsafePathError) as exc:
            log.warning("could not remove unpacked copy of %s at %s: %s", source.name, work_dir, exc)


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
