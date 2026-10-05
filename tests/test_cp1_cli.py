"""CP1: CLI skeleton and resumable run engine.

All runs go through the public entry point ``a2m.cli.main(argv, stages=...)``
(see tests/conftest.py for the stage contract), plus one subprocess call to
the installed ``a2m`` console script. Every CLI run passes ``--llm fake
--no-runtime`` and writes only under tmp_path.

Exit codes (orchestrator decision): 0 when the batch finishes, 1 when any
proxy crashed, 2 for usage errors.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
ALL3 = {"alpha", "beta", "gamma"}


# ---------------------------------------------------------------- helpers


def base_argv(exports: Path, results: Path, *extra: str) -> list[str]:
    return ["migrate", str(exports), "--out", str(results), "--llm", "fake", "--no-runtime", *extra]


def done_markers(results: Path) -> dict[str, list[Path]]:
    """Find .done markers anywhere under results, keyed by the proxy they belong to.

    A marker is either a file named ``.done`` (it belongs to its parent folder's
    name) or a file named ``<name>.done`` / ``.<name>.done``. No layout is
    assumed, because later checkpoints move finished proxies into bucket folders.
    """
    found: dict[str, list[Path]] = {}
    if not results.exists():
        return found
    for path in results.rglob("*"):
        if not path.is_file():
            continue
        if path.name == ".done":
            owner = path.parent.name
        elif path.name.endswith(".done"):
            owner = path.name[: -len(".done")].lstrip(".")
        else:
            continue
        found.setdefault(owner, []).append(path)
    return found


def marker_names(results: Path) -> set[str]:
    return set(done_markers(results))


def run_log(results: Path) -> str:
    log = results / "run.log"
    assert log.is_file(), f"expected {log} to exist"
    return log.read_text(encoding="utf-8")


def log_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def snapshot(folder: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()
    }


def new_log_text(before: str, after: str) -> str:
    """The part of run.log written by the latest run (handles append or overwrite)."""
    if after.startswith(before):
        return after[len(before) :]
    return after


def assert_no_traceback(*streams: str) -> None:
    for s in streams:
        assert "Traceback (most recent call last)" not in s, s


# ---------------------------------------------------------------- cases


def test_CP1_T01_console_script_shows_help_for_migrate(tmp_path: Path, subprocess_env: dict[str, str]) -> None:
    """[CP1-T01] The a2m command is installed and shows help for migrate."""
    exe = Path(sys.executable).parent / "a2m"
    assert exe.is_file(), f"console script not installed at {exe}"

    top = subprocess.run(
        [str(exe), "--help"], capture_output=True, text=True, cwd=tmp_path, env=subprocess_env, timeout=60
    )
    assert top.returncode == 0, top.stderr
    assert "migrate" in top.stdout

    sub = subprocess.run(
        [str(exe), "migrate", "--help"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=subprocess_env,
        timeout=60,
    )
    assert sub.returncode == 0, sub.stderr
    for flag in (
        "--out",
        "--only",
        "--resume",
        "--force",
        "--golden",
        "--mock-backends",
        "--max-fix-attempts",
        "--llm",
        "--no-runtime",
    ):
        assert flag in sub.stdout, f"{flag} missing from `a2m migrate --help`"


def test_CP1_T02_run_finds_zipped_and_unzipped_proxies_with_timestamps(
    mixed_exports: Path, results_dir: Path, run_cli
) -> None:
    """[CP1-T02] A run finds zipped and unzipped proxies and logs each one with a timestamp."""
    assert not results_dir.exists()
    res = run_cli(base_argv(mixed_exports, results_dir))

    assert res.code == 0, res.err
    assert results_dir.is_dir()
    text = run_log(results_dir)
    for name in ("alpha", "beta", "gamma"):
        assert name in text, f"run.log does not mention {name}"
    lines = log_lines(text)
    assert lines, "run.log is empty"
    for line in lines:
        assert TIMESTAMP_RE.match(line), f"run.log line without a leading timestamp: {line!r}"


def test_CP1_T03_every_finished_proxy_gets_a_done_marker(
    mixed_exports: Path, results_dir: Path, run_cli
) -> None:
    """[CP1-T03] Every finished proxy gets a .done marker."""
    res = run_cli(base_argv(mixed_exports, results_dir))

    assert res.code == 0, res.err
    markers = done_markers(results_dir)
    assert set(markers) == ALL3
    assert sum(len(v) for v in markers.values()) == 3


def test_CP1_T04_resume_skips_finished_proxies(mixed_exports: Path, results_dir: Path, run_cli, recorder) -> None:
    """[CP1-T04] Resume skips proxies that already finished and does the rest."""
    first = run_cli(base_argv(mixed_exports, results_dir))
    assert first.code == 0, first.err
    markers = done_markers(results_dir)
    assert set(markers) == ALL3
    for path in markers["beta"]:
        path.unlink()
    kept = {
        name: [(p, p.read_bytes(), p.stat().st_mtime_ns) for p in markers[name]] for name in ("alpha", "gamma")
    }
    log_before = run_log(results_dir)

    res = run_cli(base_argv(mixed_exports, results_dir, "--resume"), stages=[recorder])

    assert res.code == 0, res.err
    assert recorder.calls == ["beta"]
    assert marker_names(results_dir) == ALL3
    for name, entries in kept.items():
        for path, content, mtime in entries:
            assert path.is_file(), f"{name} marker {path} disappeared"
            assert path.read_bytes() == content
            assert path.stat().st_mtime_ns == mtime
    new = log_lines(new_log_text(log_before, run_log(results_dir)))
    for name in ("alpha", "gamma"):
        assert any(name in line and "skip" in line.lower() for line in new), (
            f"run.log from the resume run does not say {name} was skipped:\n" + "\n".join(new)
        )


def test_CP1_T05_force_redoes_finished_proxies(mixed_exports: Path, results_dir: Path, run_cli, recorder) -> None:
    """[CP1-T05] Force redoes proxies that already finished."""
    first = run_cli(base_argv(mixed_exports, results_dir))
    assert first.code == 0, first.err
    assert marker_names(results_dir) == ALL3

    res = run_cli(base_argv(mixed_exports, results_dir, "--force"), stages=[recorder])

    assert res.code == 0, res.err
    assert sorted(recorder.calls) == ["alpha", "beta", "gamma"]
    assert marker_names(results_dir) == ALL3


def test_CP1_T06_only_processes_the_named_proxy(mixed_exports: Path, results_dir: Path, run_cli, recorder) -> None:
    """[CP1-T06] --only processes just the named proxy."""
    res = run_cli(base_argv(mixed_exports, results_dir, "--only", "beta"), stages=[recorder])

    assert res.code == 0, res.err
    assert recorder.calls == ["beta"]
    assert marker_names(results_dir) == {"beta"}


def _crash_run(mixed_exports: Path, results_dir: Path, run_cli, crashing_stage, recorder):
    return run_cli(base_argv(mixed_exports, results_dir), stages=[crashing_stage, recorder])


def test_CP1_T07_one_crash_does_not_stop_the_batch(
    mixed_exports: Path, results_dir: Path, run_cli, crashing_stage, recorder
) -> None:
    """[CP1-T07] One proxy crashing does not stop the others."""
    res = _crash_run(mixed_exports, results_dir, run_cli, crashing_stage, recorder)

    # The run returns normally (run_cli would propagate a RuntimeError) with the crash exit code.
    assert res.code == 1, res.err
    assert sorted(recorder.calls) == ["alpha", "gamma"]
    assert marker_names(results_dir) == {"alpha", "gamma"}
    lines = log_lines(run_log(results_dir))
    crash_lines = [i for i, line in enumerate(lines) if "beta" in line and "boom in beta" in line]
    assert crash_lines, "run.log has no entry naming beta with 'boom in beta'"
    assert_no_traceback(res.err)


def test_CP1_T08_crashed_proxy_is_retried_on_resume(
    mixed_exports: Path, results_dir: Path, run_cli, crashing_stage, recorder
) -> None:
    """[CP1-T08] A proxy that crashed is retried on --resume."""
    crash = _crash_run(mixed_exports, results_dir, run_cli, crashing_stage, recorder)
    assert crash.code == 1, crash.err
    assert marker_names(results_dir) == {"alpha", "gamma"}

    second = type(recorder)()  # a fresh recording stage, no crash this time
    res = run_cli(base_argv(mixed_exports, results_dir, "--resume"), stages=[second])

    assert res.code == 0, res.err
    assert second.calls == ["beta"]
    assert marker_names(results_dir) == ALL3


def test_CP1_T09_existing_output_without_resume_or_force_stops(
    mixed_exports: Path, results_dir: Path, run_cli, recorder
) -> None:
    """[CP1-T09] Re-running into a results folder that already has output stops with a clear message."""
    first = run_cli(base_argv(mixed_exports, results_dir, "--only", "alpha"))
    assert first.code == 0, first.err
    assert (results_dir / "run.log").is_file()
    assert marker_names(results_dir) == {"alpha"}
    before = snapshot(results_dir)

    res = run_cli(base_argv(mixed_exports, results_dir), stages=[recorder])

    assert res.code == 2, res.err
    err_lines = [line for line in res.err.splitlines() if line.strip()]
    assert len(err_lines) == 1, f"expected one stderr line, got: {res.err!r}"
    msg = err_lines[0]
    assert str(results_dir) in msg
    assert "--resume" in msg
    assert "--force" in msg
    assert_no_traceback(res.err, res.out)
    assert snapshot(results_dir) == before
    assert recorder.calls == []


def test_CP1_T10_empty_precreated_results_folder_is_fine(
    mixed_exports: Path, results_dir: Path, run_cli
) -> None:
    """[CP1-T10] An empty results folder created in advance is fine."""
    results_dir.mkdir()

    res = run_cli(base_argv(mixed_exports, results_dir))

    assert res.code == 0, res.err
    assert (results_dir / "run.log").is_file()
    assert marker_names(results_dir) == ALL3


def test_CP1_T11_resume_and_force_together_are_rejected(
    mixed_exports: Path, results_dir: Path, run_cli, recorder
) -> None:
    """[CP1-T11] --resume and --force together are rejected."""
    res = run_cli(base_argv(mixed_exports, results_dir, "--resume", "--force"), stages=[recorder])

    assert res.code == 2
    assert "--resume" in res.err
    assert "--force" in res.err
    assert not results_dir.exists()
    assert recorder.calls == []


def test_CP1_T12_zip_slip_dotdot_member_is_refused(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, make_zip, evil_dotdot_zip_member
) -> None:
    """[CP1-T12] A zip that tries to write outside its folder with ../ is refused."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "alpha")
    make_zip(exports, "evil-dotdot", extra_members=[evil_dotdot_zip_member])

    res = run_cli(base_argv(exports, results_dir))

    assert_no_traceback(res.err, res.out)
    assert list(tmp_path.rglob("escaped.txt")) == []
    assert not (tmp_path.parent / "escaped.txt").exists()
    lines = log_lines(run_log(results_dir))
    assert any(
        "evil-dotdot" in line and ("unsafe" in line.lower() or "refused" in line.lower()) for line in lines
    ), "run.log does not say evil-dotdot was refused as unsafe:\n" + "\n".join(lines)
    markers = marker_names(results_dir)
    assert "evil-dotdot" not in markers
    assert markers == {"alpha"}


def test_CP1_T13_zip_with_absolute_member_is_refused(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, make_zip, evil_abs_zip_member
) -> None:
    """[CP1-T13] A zip with an absolute-path entry is refused."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "alpha")
    make_zip(exports, "evil-abs", extra_members=[evil_abs_zip_member])

    res = run_cli(base_argv(exports, results_dir))

    assert_no_traceback(res.err, res.out)
    assert not (tmp_path / "abs-escaped.txt").exists()
    assert list(results_dir.rglob("abs-escaped.txt")) == []
    lines = log_lines(run_log(results_dir))
    assert any(
        "evil-abs" in line and ("unsafe" in line.lower() or "refused" in line.lower()) for line in lines
    ), "run.log does not say evil-abs was refused as unsafe:\n" + "\n".join(lines)
    markers = marker_names(results_dir)
    assert "evil-abs" not in markers
    assert markers == {"alpha"}


def test_CP1_T14_corrupt_zip_is_logged_and_batch_continues(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, broken_zip_bytes
) -> None:
    """[CP1-T14] A corrupt zip file is logged and the batch carries on."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "alpha")
    (exports / "broken.zip").write_bytes(broken_zip_bytes)
    make_bundle(exports, "gamma")

    res = run_cli(base_argv(exports, results_dir))

    assert res.code in (0, 1), res.err
    assert_no_traceback(res.err, res.out)
    lines = log_lines(run_log(results_dir))
    assert any("broken" in line and "error" in line.lower() for line in lines), (
        "run.log has no error entry naming broken:\n" + "\n".join(lines)
    )
    assert marker_names(results_dir) == {"alpha", "gamma"}


def test_CP1_T15_only_with_unknown_name_stops(mixed_exports: Path, results_dir: Path, run_cli, recorder) -> None:
    """[CP1-T15] --only with a name that does not exist stops clearly."""
    res = run_cli(base_argv(mixed_exports, results_dir, "--only", "delta"), stages=[recorder])

    assert res.code == 2, res.err
    assert "delta" in res.err
    assert_no_traceback(res.err, res.out)
    assert recorder.calls == []
    assert marker_names(results_dir) == set()


def test_CP1_T16_missing_input_folder_gives_clear_error(tmp_path: Path, results_dir: Path, run_cli) -> None:
    """[CP1-T16] A missing input folder gives a clear error."""
    missing = tmp_path / "does-not-exist"

    res = run_cli(base_argv(missing, results_dir))

    assert res.code == 2, res.err
    err_lines = [line for line in res.err.splitlines() if line.strip()]
    assert len(err_lines) == 1, f"expected one stderr line, got: {res.err!r}"
    assert "does-not-exist" in err_lines[0]
    assert_no_traceback(res.err, res.out)
    assert not results_dir.exists()


def test_CP1_T17_input_folder_with_no_proxies_says_so(tmp_path: Path, results_dir: Path, run_cli) -> None:
    """[CP1-T17] An input folder with no proxies says so."""
    exports = tmp_path / "empty-exports"
    exports.mkdir()

    res = run_cli(base_argv(exports, results_dir))

    log_text = (results_dir / "run.log").read_text(encoding="utf-8") if (results_dir / "run.log").is_file() else ""
    combined = "\n".join([res.out, res.err, log_text]).lower()
    assert re.search(r"\bno\b[^\n]*\bprox", combined), f"no 'no proxies found' message in output:\n{combined}"
    assert_no_traceback(res.err, res.out)
    assert marker_names(results_dir) == set()


def test_CP1_T18_non_bundle_items_are_not_proxies(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle
) -> None:
    """[CP1-T18] Files and folders that are not proxy bundles are not treated as proxies."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "alpha")
    (exports / "notes.txt").write_text("just some notes\n", encoding="utf-8")
    (exports / "docs").mkdir()
    (exports / "docs" / "README.md").write_text("# docs\n", encoding="utf-8")

    res = run_cli(base_argv(exports, results_dir))

    assert res.code == 0, res.err
    assert marker_names(results_dir) == {"alpha"}
    for line in log_lines(run_log(results_dir)):
        low = line.lower()
        if "notes" in low or "docs" in low:
            assert any(word in low for word in ("not ", "skip", "ignor")), (
                f"run.log reports a non-bundle item as a proxy: {line!r}"
            )


def test_CP1_T19_input_folder_is_never_changed(mixed_exports: Path, results_dir: Path, run_cli) -> None:
    """[CP1-T19] The input folder is never changed by a run."""
    before = snapshot(mixed_exports)

    res = run_cli(base_argv(mixed_exports, results_dir))

    assert res.code == 0, res.err
    assert snapshot(mixed_exports) == before
    assert not (mixed_exports / "beta").exists()
    assert sorted(p.name for p in mixed_exports.iterdir()) == ["alpha", "beta.zip", "gamma"]


@pytest.mark.parametrize(
    ("extra", "flag"),
    [
        pytest.param(["--llm", "openai"], "--llm", id="CP1-T20-llm-openai"),
        pytest.param(["--max-fix-attempts", "-1"], "--max-fix-attempts", id="CP1-T20-max-fix-negative"),
        pytest.param(["--max-fix-attempts", "abc"], "--max-fix-attempts", id="CP1-T20-max-fix-abc"),
    ],
)
def test_CP1_T20_bad_flag_values_are_rejected(
    mixed_exports: Path, results_dir: Path, run_cli, recorder, extra: list[str], flag: str
) -> None:
    """[CP1-T20] Bad flag values are rejected before anything runs."""
    argv = ["migrate", str(mixed_exports), "--out", str(results_dir), "--no-runtime"]
    if "--llm" not in extra:
        argv += ["--llm", "fake"]
    argv += extra

    res = run_cli(argv, stages=[recorder])

    assert res.code == 2
    assert flag in res.err
    assert "usage" in res.err.lower()
    assert not results_dir.exists()
    assert recorder.calls == []


def test_CP1_T21_flags_for_later_stages_are_accepted(
    mixed_exports: Path, results_dir: Path, tmp_path: Path, run_cli
) -> None:
    """[CP1-T21] Flags for later stages are accepted now."""
    recordings = tmp_path / "recordings"
    recordings.mkdir()

    res = run_cli(
        base_argv(
            mixed_exports,
            results_dir,
            "--golden",
            str(recordings),
            "--mock-backends",
            "--max-fix-attempts",
            "0",
        )
    )

    assert res.code == 0, res.err
    assert marker_names(results_dir) == ALL3


# ---------------------------------------------------------------- adversarial round 01 additions
# New imports for the cases below live here so no existing line changes.

import errno  # noqa: E402

from a2m import layout as a2m_layout  # noqa: E402
from a2m.errors import UnsafePathError  # noqa: E402


@pytest.mark.parametrize(
    "stem",
    [
        pytest.param("..", id="CP1-T22-three-dots-zip"),
        pytest.param(".", id="CP1-T22-two-dots-zip"),
    ],
)
def test_CP1_T22_dot_named_zips_are_refused_and_nothing_outside_results_is_deleted(
    tmp_path: Path, run_cli, make_bundle, make_zip, stem: str
) -> None:
    """[CP1-T22] A zip whose stem is '.' or '..' is refused; sibling and parent folders survive."""
    victim = tmp_path / "victim"
    exports = victim / "exports"
    exports.mkdir(parents=True)
    make_bundle(exports, "alpha")
    zip_path = make_zip(exports, stem)
    assert zip_path.name == f"{stem}.zip"
    assert zip_path.stem == stem
    precious = victim / "precious.txt"
    precious.write_text("keep me\n", encoding="utf-8")
    results = victim / "results"
    exports_before = snapshot(exports)

    res = run_cli(base_argv(exports, results))

    assert res.code == 0, res.err
    assert_no_traceback(res.err, res.out)
    assert precious.is_file()
    assert precious.read_text(encoding="utf-8") == "keep me\n"
    assert snapshot(exports) == exports_before
    assert sorted(p.name for p in victim.iterdir()) == ["exports", "precious.txt", "results"]
    text = run_log(results)
    lines = log_lines(text)
    assert any(zip_path.name in line and "refused" in line.lower() and "error" in line.lower() for line in lines), (
        f"run.log does not say {zip_path.name} was refused:\n" + text
    )
    assert "run finished" in text
    assert not (results / ".done").exists()
    assert marker_names(results) == {"alpha"}
    assert "1 done" in res.out
    assert "1 refused" in res.out


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("", id="CP1-T23-empty"),
        pytest.param(".", id="CP1-T23-dot"),
        pytest.param("..", id="CP1-T23-dotdot"),
        pytest.param("a/b", id="CP1-T23-slash"),
        pytest.param("a\\b", id="CP1-T23-backslash"),
        pytest.param("a\x00b", id="CP1-T23-nul"),
        pytest.param("run.log", id="CP1-T23-run-log"),
        pytest.param("RUN.LOG", id="CP1-T23-run-log-upper"),
        pytest.param("run.log.", id="CP1-T23-run-log-trailing-dot"),
        pytest.param(".a2m-work", id="CP1-T23-work-root"),
        pytest.param(".A2M-Work", id="CP1-T23-work-root-mixed-case"),
        pytest.param(".a2m-results", id="CP1-T23-results-marker"),
        pytest.param(".hidden", id="CP1-T23-leading-dot"),
    ],
)
def test_CP1_T23_results_paths_refuse_names_that_leave_their_folder(tmp_path: Path, name: str) -> None:
    """[CP1-T23] The results-folder path helpers refuse any name that is not a plain child folder."""
    out = tmp_path / "results"

    with pytest.raises(UnsafePathError):
        a2m_layout.proxy_out_dir(out, name)
    with pytest.raises(UnsafePathError):
        a2m_layout.proxy_work_dir(out, name)
    with pytest.raises(UnsafePathError):
        a2m_layout.done_marker_path(out, name)
    assert a2m_layout.proxy_out_dir(out, "alpha") == out / "alpha"
    assert not out.exists()


def test_CP1_T24_engine_never_deletes_outside_results_even_if_discovery_lets_a_bad_name_through(
    tmp_path: Path, run_cli, make_bundle, make_zip, monkeypatch: pytest.MonkeyPatch, recorder
) -> None:
    """[CP1-T24] Defense in depth: with name validation in discovery disabled, '..' still deletes nothing."""
    monkeypatch.setattr("a2m.discovery.unsafe_name_reason", lambda name: None)
    victim = tmp_path / "victim"
    exports = victim / "exports"
    exports.mkdir(parents=True)
    make_bundle(exports, "alpha")
    make_zip(exports, "..")
    precious = victim / "precious.txt"
    precious.write_text("keep me\n", encoding="utf-8")
    results = victim / "results"
    exports_before = snapshot(exports)

    res = run_cli(base_argv(exports, results), stages=[recorder])

    assert res.code == 1, res.err
    assert precious.read_text(encoding="utf-8") == "keep me\n"
    assert snapshot(exports) == exports_before
    assert sorted(p.name for p in victim.iterdir()) == ["exports", "precious.txt", "results"]
    assert recorder.calls == ["alpha"]
    assert marker_names(results) == {"alpha"}
    text = run_log(results)
    assert any("failed" in line.lower() and ".." in line for line in log_lines(text)), text
    assert "run finished" in text


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        pytest.param("zip", "run.log", id="CP1-T25-zip-run-log"),
        pytest.param("zip", "RUN.LOG", id="CP1-T25-zip-run-log-upper"),
        pytest.param("folder", ".a2m-work", id="CP1-T25-folder-work-root"),
        pytest.param("folder", ".A2M-WORK", id="CP1-T25-folder-work-root-upper"),
        pytest.param("zip", ".a2m-results", id="CP1-T25-zip-results-marker"),
    ],
)
def test_CP1_T25_reserved_proxy_names_are_refused_case_insensitively(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, make_zip, recorder, kind: str, name: str
) -> None:
    """[CP1-T25] A proxy named like one of a2m's own files (any letter case) is refused and logged."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "alpha")
    if kind == "zip":
        item = make_zip(exports, name)
    else:
        item = make_bundle(exports, name)
    assert item.exists()

    res = run_cli(base_argv(exports, results_dir), stages=[recorder])

    assert res.code == 0, res.err
    assert_no_traceback(res.err, res.out)
    assert (results_dir / "run.log").is_file()
    text = run_log(results_dir)
    assert "run finished" in text
    assert any(
        item.name in line and "refused" in line.lower() and "error" in line.lower() for line in log_lines(text)
    ), f"run.log does not say {item.name} was refused:\n" + text
    assert recorder.calls == ["alpha"]
    assert marker_names(results_dir) == {"alpha"}
    if name not in a2m_layout.RESERVED_NAMES:
        assert not (results_dir / name).exists()
    assert "1 refused" in res.out


def test_CP1_T26_disk_error_while_unpacking_is_a_failure_not_a_refusal(
    mixed_exports: Path, results_dir: Path, run_cli, recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP1-T26] A write error while unpacking a zip under --out is a failed proxy (exit 1), not a bad bundle."""
    real_open = Path.open

    def failing_open(self: Path, mode: str = "r", *args, **kwargs):
        if "w" in mode and a2m_layout.WORK_DIR_NAME in self.parts:
            raise OSError(errno.ENOSPC, "No space left on device", str(self))
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)

    res = run_cli(base_argv(mixed_exports, results_dir), stages=[recorder])

    assert res.code == 1, res.err
    assert_no_traceback(res.err)
    lines = log_lines(run_log(results_dir))
    assert any("beta" in line and "failed" in line.lower() and "No space left" in line for line in lines), (
        "run.log has no 'failed' entry for beta naming the disk error:\n" + "\n".join(lines)
    )
    assert not any("beta" in line and "refused" in line.lower() for line in lines)
    assert sorted(recorder.calls) == ["alpha", "gamma"]
    assert marker_names(results_dir) == {"alpha", "gamma"}
    assert "1 failed" in res.out
    assert "0 refused" in res.out


@pytest.mark.parametrize(
    "flag",
    [pytest.param("--force", id="CP1-T27-force"), pytest.param("--resume", id="CP1-T27-resume")],
)
def test_CP1_T27_foreign_folder_with_a_run_log_is_not_reused(
    mixed_exports: Path, results_dir: Path, run_cli, recorder, flag: str
) -> None:
    """[CP1-T27] A non-empty folder with a run.log but no a2m marker is refused even with --force/--resume."""
    results_dir.mkdir()
    (results_dir / "run.log").write_text("log from another tool\n", encoding="utf-8")
    (results_dir / "alpha").mkdir()
    (results_dir / "alpha" / "keep.txt").write_text("not a2m output\n", encoding="utf-8")
    before = snapshot(results_dir)

    res = run_cli(base_argv(mixed_exports, results_dir, flag), stages=[recorder])

    assert res.code == 2, res.err
    err_lines = [line for line in res.err.splitlines() if line.strip()]
    assert len(err_lines) == 1, f"expected one stderr line, got: {res.err!r}"
    assert str(results_dir) in err_lines[0]
    assert_no_traceback(res.err, res.out)
    assert snapshot(results_dir) == before
    assert sorted(p.name for p in results_dir.iterdir()) == ["alpha", "run.log"]
    assert recorder.calls == []


def test_CP1_T28_folder_and_zip_with_the_same_name_are_both_refused(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, make_zip, recorder
) -> None:
    """[CP1-T28] A folder and a zip with the same proxy name: neither is processed, both are logged as errors."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "alpha")
    make_zip(exports, "alpha")
    make_bundle(exports, "gamma")

    res = run_cli(base_argv(exports, results_dir), stages=[recorder])

    assert res.code == 0, res.err
    assert recorder.calls == ["gamma"]
    assert marker_names(results_dir) == {"gamma"}
    assert not (results_dir / "alpha").exists()
    errors = [line for line in log_lines(run_log(results_dir)) if "error" in line.lower() and "alpha" in line]
    assert len(errors) >= 2, "expected an error line for each of alpha/ and alpha.zip:\n" + "\n".join(errors)
    assert any("alpha.zip" in line for line in errors)
    assert all("conflict" in line.lower() for line in errors)
    assert "2 refused" in res.out


def test_CP1_T29_names_differing_only_in_case_are_both_refused(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, recorder
) -> None:
    """[CP1-T29] Beta/ and beta/ map to the same folder on some systems, so both are refused and logged."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "Beta")
    if (exports / "beta").exists():
        pytest.skip("file system is case-insensitive; Beta/ and beta/ cannot both exist")
    make_bundle(exports, "beta")
    make_bundle(exports, "gamma")

    res = run_cli(base_argv(exports, results_dir), stages=[recorder])

    assert res.code == 0, res.err
    assert recorder.calls == ["gamma"]
    assert marker_names(results_dir) == {"gamma"}
    assert not (results_dir / "Beta").exists()
    assert not (results_dir / "beta").exists()
    lines = log_lines(run_log(results_dir))
    for name in ("Beta", "beta"):
        assert any(
            f" {name} " in line and "error" in line.lower() and "conflict" in line.lower() for line in lines
        ), f"run.log has no conflict error for {name}:\n" + "\n".join(lines)
    assert "2 refused" in res.out


def test_CP1_T30_shared_flow_bundles_are_logged_not_processed(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, recorder
) -> None:
    """[CP1-T30] Shared flow bundles (folder or zip) are logged as shared flows and not processed as proxies."""
    import zipfile

    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "alpha")
    sf_folder = exports / "sf-folder" / "sharedflowbundle"
    sf_folder.mkdir(parents=True)
    (sf_folder / "sf-folder.xml").write_text('<SharedFlowBundle name="sf-folder"/>\n', encoding="utf-8")
    with zipfile.ZipFile(exports / "sf-zip.zip", "w") as zf:
        zf.writestr("sharedflowbundle/sf-zip.xml", '<SharedFlowBundle name="sf-zip"/>\n')

    res = run_cli(base_argv(exports, results_dir), stages=[recorder])

    assert res.code == 0, res.err
    assert recorder.calls == ["alpha"]
    assert marker_names(results_dir) == {"alpha"}
    lines = log_lines(run_log(results_dir))
    for name in ("sf-folder", "sf-zip"):
        assert any(name in line and "shared flow" in line.lower() for line in lines), (
            f"run.log does not log {name} as a shared flow:\n" + "\n".join(lines)
        )
        assert not any(f"processing {name}" in line for line in lines)
        assert not (results_dir / name).exists()


@pytest.mark.parametrize(
    "where",
    [
        pytest.param("same", id="CP1-T31-out-is-input"),
        pytest.param("inside", id="CP1-T31-out-inside-input"),
        pytest.param("contains", id="CP1-T31-out-contains-input"),
    ],
)
def test_CP1_T31_results_folder_overlapping_the_input_is_refused(
    tmp_path: Path, run_cli, make_bundle, make_zip, recorder, where: str
) -> None:
    """[CP1-T31] --out equal to, inside, or containing the input folder stops with exit 2 and writes nothing."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "alpha")
    make_zip(exports, "beta")
    out = {"same": exports, "inside": exports / "results", "contains": tmp_path}[where]
    paths_before = sorted(str(p) for p in tmp_path.rglob("*"))
    files_before = snapshot(tmp_path)

    res = run_cli(base_argv(exports, out), stages=[recorder])

    assert res.code == 2, res.err
    err_lines = [line for line in res.err.splitlines() if line.strip()]
    assert len(err_lines) == 1, f"expected one stderr line, got: {res.err!r}"
    assert_no_traceback(res.err, res.out)
    assert sorted(str(p) for p in tmp_path.rglob("*")) == paths_before
    assert snapshot(tmp_path) == files_before
    assert recorder.calls == []


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param([], id="CP1-T32-no-flag"),
        pytest.param(["--resume"], id="CP1-T32-resume"),
        pytest.param(["--force"], id="CP1-T32-force"),
    ],
)
def test_CP1_T32_non_empty_foreign_results_folder_is_refused_and_unchanged(
    mixed_exports: Path, results_dir: Path, run_cli, recorder, extra: list[str]
) -> None:
    """[CP1-T32] A non-empty results folder that a2m did not create is refused with exit 2 and left as is."""
    results_dir.mkdir()
    (results_dir / "notes.txt").write_text("someone else's notes\n", encoding="utf-8")
    (results_dir / "gamma").mkdir()
    (results_dir / "gamma" / "data.bin").write_bytes(b"\x00\x01\x02")
    before = snapshot(results_dir)

    res = run_cli(base_argv(mixed_exports, results_dir, *extra), stages=[recorder])

    assert res.code == 2, res.err
    err_lines = [line for line in res.err.splitlines() if line.strip()]
    assert len(err_lines) == 1, f"expected one stderr line, got: {res.err!r}"
    assert str(results_dir) in err_lines[0]
    assert_no_traceback(res.err, res.out)
    assert snapshot(results_dir) == before
    assert sorted(p.name for p in results_dir.iterdir()) == ["gamma", "notes.txt"]
    assert recorder.calls == []


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("dotdot", id="CP1-T33-zip-slip-dotdot"),
        pytest.param("abs", id="CP1-T33-zip-absolute-member"),
        pytest.param("corrupt", id="CP1-T33-corrupt-zip"),
    ],
)
def test_CP1_T33_refused_bundles_exit_zero(
    tmp_path: Path,
    results_dir: Path,
    run_cli,
    make_bundle,
    make_zip,
    evil_dotdot_zip_member,
    evil_abs_zip_member,
    broken_zip_bytes,
    kind: str,
) -> None:
    """[CP1-T33] An unsafe or corrupt zip is refused, not a crash: the batch exits 0."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "alpha")
    if kind == "dotdot":
        make_zip(exports, "bad", extra_members=[evil_dotdot_zip_member])
    elif kind == "abs":
        make_zip(exports, "bad", extra_members=[evil_abs_zip_member])
    else:
        (exports / "bad.zip").write_bytes(broken_zip_bytes)

    res = run_cli(base_argv(exports, results_dir))

    assert res.code == 0, res.err
    assert_no_traceback(res.err, res.out)
    assert marker_names(results_dir) == {"alpha"}
    assert any("bad" in line and "refused" in line.lower() for line in log_lines(run_log(results_dir)))
    assert "1 refused" in res.out
    assert "0 failed" in res.out


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("empty", id="CP1-T34-empty-folder"),
        pytest.param("noise", id="CP1-T34-only-non-bundles"),
    ],
)
def test_CP1_T34_no_proxies_is_a_usage_error_and_writes_nothing(
    tmp_path: Path, results_dir: Path, run_cli, content: str
) -> None:
    """[CP1-T34] An input folder with no proxies exits 2 and creates no results folder."""
    exports = tmp_path / "empty-exports"
    exports.mkdir()
    if content == "noise":
        (exports / "notes.txt").write_text("just some notes\n", encoding="utf-8")
        (exports / "docs").mkdir()

    res = run_cli(base_argv(exports, results_dir))

    assert res.code == 2, res.err
    assert_no_traceback(res.err, res.out)
    assert re.search(r"\bno\b[^\n]*\bprox", res.err.lower()), res.err
    assert not results_dir.exists()


# ---------------------------------------------------------------- adversarial round 02 additions
# New imports for the cases below live here so no existing line changes.

import ast  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import zipfile  # noqa: E402

from a2m import safefs as a2m_safefs  # noqa: E402

LOG_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{4} (DEBUG|INFO|WARNING|ERROR|CRITICAL) +")
needs_non_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="file permissions do not apply to root"
)


def _one_stderr_line(err: str) -> str:
    lines = [line for line in err.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected one stderr line, got: {err!r}"
    return lines[0]


def _tree_state(folder: Path) -> dict[str, object]:
    """Every entry under ``folder`` (links not followed): file bytes, link targets, folders."""
    state: dict[str, object] = {}
    for path in sorted(folder.rglob("*")):
        rel = str(path.relative_to(folder))
        if path.is_symlink():
            state[rel] = ("link", os.readlink(path))
        elif path.is_dir():
            state[rel] = ("dir",)
        else:
            state[rel] = ("file", path.read_bytes())
    return state


def _write_zip_with_undecodable_member(path: Path) -> None:
    """A zip whose member name is flagged as UTF-8 but holds the bytes ff fe."""
    placeholder = "apiproxy/\u00e9.xml".encode("utf-8")  # non-ASCII, so zipfile sets the UTF-8 flag
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(zipfile.ZipInfo(placeholder.decode("utf-8"), (2026, 1, 1, 0, 0, 0)), "<APIProxy/>\n")
    data = path.read_bytes()
    assert data.count(placeholder) == 2
    path.write_bytes(data.replace(placeholder, b"apiproxy/\xff\xfe.xml"))


def _write_raw_zip(path: Path, members: list[tuple[str, str]]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in members:
            zf.writestr(zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0)), text)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("a\nb", id="CP1-T35-newline"),
        pytest.param("a\tb", id="CP1-T35-tab"),
        pytest.param("a\rb", id="CP1-T35-carriage-return"),
        pytest.param("a\x1bb", id="CP1-T35-escape"),
        pytest.param("a\x7fb", id="CP1-T35-delete"),
        pytest.param("a\x85b", id="CP1-T35-next-line"),
        pytest.param("a b", id="CP1-T35-line-separator"),
        pytest.param("a‎b", id="CP1-T35-format-char"),
        pytest.param("a\udcffb", id="CP1-T35-undecodable-byte"),
    ],
)
def test_CP1_T35_proxy_names_with_control_characters_are_refused(tmp_path: Path, name: str) -> None:
    """[CP1-T35] Names with control, line-break, format or undecodable characters are unsafe."""
    out = tmp_path / "results"

    assert a2m_layout.unsafe_name_reason(name) is not None
    assert "\n" not in a2m_layout.unsafe_name_reason(name)
    with pytest.raises(UnsafePathError):
        a2m_layout.proxy_out_dir(out, name)
    with pytest.raises(UnsafePathError):
        a2m_layout.proxy_work_dir(out, name)
    assert a2m_layout.unsafe_name_reason("order api v2") is None
    assert not out.exists()


def test_CP1_T36_names_cannot_forge_run_log_entries(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, recorder
) -> None:
    """[CP1-T36] A folder name holding a newline and a fake log entry is refused and stays on one line."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "alpha")
    forged = "x\n2026-10-01 00:00:00-0400 INFO    done gamma"
    make_bundle(exports, forged)
    (exports / "y\n2026-10-01 00:00:00-0400 INFO    done delta").mkdir()

    res = run_cli(base_argv(exports, results_dir), stages=[recorder])

    assert res.code == 0, res.err
    assert_no_traceback(res.err, res.out)
    assert res.err == ""
    assert recorder.calls == ["alpha"]
    assert marker_names(results_dir) == {"alpha"}
    assert all("\n" not in p.name for p in results_dir.rglob("*"))
    text = run_log(results_dir)
    for line in log_lines(text):
        assert LOG_PREFIX_RE.match(line), f"line without its own timestamp prefix: {line!r}"
        assert not TIMESTAMP_RE.match(LOG_PREFIX_RE.sub("", line, count=1)), f"forged entry: {line!r}"
    assert not any(line.endswith("done gamma") and "refused" not in line for line in log_lines(text))
    assert any("refused" in line and "ERROR" in line and "done gamma" in line for line in log_lines(text))
    assert "1 refused" in res.out


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("undecodable-zip-member", id="CP1-T37-undecodable-zip-member-name"),
        pytest.param("locked-folder", id="CP1-T37-unreadable-proxy-folder", marks=needs_non_root),
        pytest.param("locked-zip", id="CP1-T37-unreadable-zip", marks=needs_non_root),
        pytest.param("undecodable-folder-name", id="CP1-T37-undecodable-folder-name"),
        pytest.param("file-under-file", id="CP1-T37-zip-member-under-a-file"),
        pytest.param("file-and-folder-entry", id="CP1-T37-zip-file-and-folder-entry"),
        pytest.param("duplicate-member", id="CP1-T37-zip-duplicate-member"),
    ],
)
def test_CP1_T37_one_unreadable_input_item_is_refused_and_the_batch_goes_on(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, recorder, kind: str
) -> None:
    """[CP1-T37] Any input item that cannot be read is refused and logged; the rest finish, exit 0."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "alpha")
    locked: Path | None = None
    if kind == "undecodable-zip-member":
        _write_zip_with_undecodable_member(exports / "bad.zip")
    elif kind == "locked-folder":
        locked = make_bundle(exports, "bad-locked")
    elif kind == "locked-zip":
        _write_raw_zip(exports / "bad.zip", [("apiproxy/bad.xml", "<APIProxy/>\n")])
        locked = exports / "bad.zip"
    elif kind == "undecodable-folder-name":
        raw = os.fsencode(exports) + b"/bad\xff"
        try:
            os.mkdir(raw)
        except OSError:
            pytest.skip("file system does not accept undecodable file names")
        os.mkdir(raw + b"/apiproxy")
    elif kind == "file-under-file":
        _write_raw_zip(
            exports / "bad.zip", [("apiproxy/bad.xml", "<APIProxy/>\n"), ("apiproxy/bad.xml/inner.xml", "x")]
        )
    elif kind == "file-and-folder-entry":
        _write_raw_zip(exports / "bad.zip", [("apiproxy/bad.xml", "<APIProxy/>\n"), ("apiproxy/bad.xml/", "")])
    else:
        with pytest.warns(UserWarning):
            _write_raw_zip(exports / "bad.zip", [("apiproxy/bad.xml", "a"), ("apiproxy/bad.xml", "b")])
    if locked is not None:
        os.chmod(locked, 0)
    try:
        res = run_cli(base_argv(exports, results_dir), stages=[recorder])
    finally:
        if locked is not None:
            os.chmod(locked, 0o700)

    assert res.code == 0, res.err
    assert_no_traceback(res.err, res.out)
    assert res.err == ""
    assert recorder.calls == ["alpha"]
    assert marker_names(results_dir) == {"alpha"}
    text = run_log(results_dir)
    assert "Traceback" not in text
    assert any("bad" in line and "refused" in line and "ERROR" in line for line in log_lines(text)), text
    assert "run finished" in text
    assert "1 refused" in res.out
    assert "0 failed" in res.out


@pytest.mark.parametrize(
    "which",
    [
        pytest.param("input", id="CP1-T38-unreadable-input-folder"),
        pytest.param("results", id="CP1-T38-unreadable-results-folder"),
    ],
)
@needs_non_root
def test_CP1_T38_unreadable_input_or_results_folder_is_a_usage_error(
    mixed_exports: Path, results_dir: Path, run_cli, recorder, which: str
) -> None:
    """[CP1-T38] An unreadable input or results folder exits 2 with one stderr line, never a traceback."""
    extra: list[str] = []
    if which == "results":
        assert run_cli(base_argv(mixed_exports, results_dir), stages=[recorder]).code == 0
        recorder.calls.clear()
        extra = ["--resume"]
    locked = mixed_exports if which == "input" else results_dir
    os.chmod(locked, 0)
    try:
        res = run_cli(base_argv(mixed_exports, results_dir, *extra), stages=[recorder])
    finally:
        os.chmod(locked, 0o700)

    assert res.code == 2, res.err
    line = _one_stderr_line(res.err)
    assert str(locked) in line
    assert "usage error" in line
    assert_no_traceback(res.err, res.out)
    assert recorder.calls == []
    if which == "input":
        assert not results_dir.exists()


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(".a2m-work", id="CP1-T39-work-root-link"),
        pytest.param("run.log", id="CP1-T39-run-log-link"),
        pytest.param(".a2m-results", id="CP1-T39-marker-link"),
    ],
)
def test_CP1_T39_links_at_a2m_entries_in_results_are_refused_and_nothing_outside_changes(
    tmp_path: Path, mixed_exports: Path, results_dir: Path, run_cli, recorder, entry: str
) -> None:
    """[CP1-T39] A symlink at .a2m-work, run.log or the marker stops the run (exit 2); its target is untouched."""
    assert run_cli(base_argv(mixed_exports, results_dir), stages=[recorder]).code == 0
    recorder.calls.clear()
    victim = tmp_path / "victim"
    (victim / "beta").mkdir(parents=True)
    (victim / "beta" / "precious.txt").write_text("keep me\n", encoding="utf-8")
    (victim / "file.txt").write_text(a2m_layout.RESULTS_MARKER_TEXT, encoding="utf-8")
    link = results_dir / entry
    if link.exists():
        link.unlink()
    link.symlink_to(victim if entry == ".a2m-work" else victim / "file.txt")
    victim_before = _tree_state(victim)
    results_before = _tree_state(results_dir)

    res = run_cli(base_argv(mixed_exports, results_dir, "--force"), stages=[recorder])

    assert res.code == 2, res.err
    line = _one_stderr_line(res.err)
    assert entry in line
    assert_no_traceback(res.err, res.out)
    assert _tree_state(victim) == victim_before
    assert _tree_state(results_dir) == results_before
    assert recorder.calls == []


def test_CP1_T40_engine_never_deletes_through_a_work_root_link_even_if_the_up_front_check_is_off(
    tmp_path: Path, mixed_exports: Path, results_dir: Path, run_cli, recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP1-T40] Defense in depth: with the results-folder check off, a linked .a2m-work still deletes nothing."""
    assert run_cli(base_argv(mixed_exports, results_dir), stages=[recorder]).code == 0
    recorder.calls.clear()
    victim = tmp_path / "victim"
    (victim / "beta").mkdir(parents=True)
    (victim / "beta" / "precious.txt").write_text("keep me\n", encoding="utf-8")
    (results_dir / ".a2m-work").symlink_to(victim)
    victim_before = _tree_state(victim)
    monkeypatch.setattr("a2m.engine._check_owned_entries", lambda out: None)

    res = run_cli(base_argv(mixed_exports, results_dir, "--force"), stages=[recorder])

    assert res.code == 1, res.err
    assert_no_traceback(res.err)
    assert _tree_state(victim) == victim_before
    assert (results_dir / ".a2m-work").is_symlink()
    assert recorder.calls == []
    assert marker_names(results_dir) == set()
    lines = log_lines(run_log(results_dir))
    assert any("failed beta" in line and "symbolic link" in line for line in lines), "\n".join(lines)
    assert "3 failed" in res.out


def _make_guard_case(tmp_path: Path, where: str) -> tuple[Path, Path, Path]:
    """(results root, target, victim folder) for one unsafe location."""
    root = tmp_path / "results"
    root.mkdir()
    victim = tmp_path / "victim"
    (victim / "x").mkdir(parents=True)
    (victim / "x" / "keep.txt").write_text("keep me\n", encoding="utf-8")
    (victim / "x" / "empty").mkdir()
    if where == "root-itself":
        target = root
    elif where == "dotdot":
        target = root / ".." / "victim" / "x"
    elif where == "sibling":
        target = tmp_path / "victim" / "x"
    elif where == "prefix-sibling":
        (tmp_path / "results2").mkdir()
        target = tmp_path / "results2" / "x"
    elif where == "link-below-root":
        (root / "link").symlink_to(victim)
        target = root / "link" / "x"
    elif where == "link-deeper":
        (root / "real").mkdir()
        (root / "real" / "link").symlink_to(victim)
        target = root / "real" / "link" / "x" / "empty"
    else:
        raise AssertionError(where)
    return root, target, victim


GUARD_LOCATIONS = ["root-itself", "dotdot", "sibling", "prefix-sibling", "link-below-root", "link-deeper"]


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param("remove", id="remove"),
        pytest.param("remove_empty_dir", id="remove-empty-dir"),
        pytest.param("make_dirs", id="make-dirs"),
        pytest.param("write_text_atomic", id="write-text"),
    ],
)
@pytest.mark.parametrize("where", [pytest.param(w, id=f"CP1-T41-{w}") for w in GUARD_LOCATIONS])
def test_CP1_T41_guarded_file_operations_refuse_anything_not_strictly_inside_without_links(
    tmp_path: Path, where: str, operation: str
) -> None:
    """[CP1-T41] Every guarded delete/write refuses targets outside the root or behind a link, changing nothing."""
    root, target, victim = _make_guard_case(tmp_path, where)
    victim_before = _tree_state(victim)
    root_before = _tree_state(root)
    call = getattr(a2m_safefs, operation)

    with pytest.raises(UnsafePathError):
        if operation == "write_text_atomic":
            call(root, target, "pwned\n")
        else:
            call(root, target)

    assert _tree_state(victim) == victim_before
    assert _tree_state(root) == root_before
    assert root.is_dir()
    assert a2m_safefs.is_regular_file(root, target / "keep.txt") is False


def test_CP1_T42_guarded_operations_treat_a_link_at_the_target_as_a_link(tmp_path: Path) -> None:
    """[CP1-T42] A link at the target is removed or replaced as a link, never followed; make_dirs refuses it."""
    root = tmp_path / "results"
    root.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep me\n", encoding="utf-8")
    (root / "dir-link").symlink_to(victim)
    (root / "file-link").symlink_to(victim / "keep.txt")
    (root / "done-link").symlink_to(victim / "keep.txt")
    victim_before = _tree_state(victim)

    assert a2m_safefs.is_regular_file(root, root / "file-link") is False
    with pytest.raises(UnsafePathError):
        a2m_safefs.make_dirs(root, root / "dir-link")
    with pytest.raises(UnsafePathError):
        a2m_safefs.make_dirs(root, root / "dir-link" / "sub")
    assert a2m_safefs.remove_empty_dir(root, root / "dir-link") is False
    assert a2m_safefs.remove(root, root / "dir-link") is True
    assert a2m_safefs.remove(root, root / "file-link") is True
    a2m_safefs.write_text_atomic(root, root / "done-link", "a2m finished x\n")

    assert _tree_state(victim) == victim_before
    assert not (root / "dir-link").exists() and not (root / "dir-link").is_symlink()
    assert not (root / "file-link").is_symlink()
    assert not (root / "done-link").is_symlink()
    assert (root / "done-link").read_text(encoding="utf-8") == "a2m finished x\n"
    assert a2m_safefs.remove(root, root / "missing") is False


_DELETING_CALLS = {
    ("os", "remove"),
    ("os", "unlink"),
    ("os", "rmdir"),
    ("os", "removedirs"),
    ("os", "replace"),
    ("os", "rename"),
    ("shutil", "rmtree"),
    ("shutil", "move"),
}
_DELETING_METHODS = {"unlink", "rmdir", "rmtree", "rename"}


def test_CP1_T43_only_the_guard_module_deletes_or_renames_files() -> None:
    """[CP1-T43] Structural: no a2m module except safefs calls a delete or rename primitive directly."""
    import a2m

    package = Path(a2m.__file__).parent
    offenders: list[str] = []
    for source in sorted(package.rglob("*.py")):
        if source.name == "safefs.py":
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in ("os", "shutil"):
                for alias in node.names:
                    if (node.module, alias.name) in _DELETING_CALLS:
                        offenders.append(f"{source.name}:{node.lineno} imports {node.module}.{alias.name}")
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            attr = node.func.attr
            owner = node.func.value
            if isinstance(owner, ast.Name) and (owner.id, attr) in _DELETING_CALLS:
                offenders.append(f"{source.name}:{node.lineno} calls {owner.id}.{attr}")
            elif attr in _DELETING_METHODS:
                offenders.append(f"{source.name}:{node.lineno} calls .{attr}()")
    assert offenders == [], "delete/rename outside a2m/safefs.py:\n" + "\n".join(offenders)


def test_CP1_T44_every_delete_in_a_run_lands_strictly_inside_results(
    tmp_path: Path, mixed_exports: Path, results_dir: Path, run_cli, make_zip, recorder, monkeypatch
) -> None:
    """[CP1-T44] Structural: across first run, --force, conflicts and a linked proxy folder, every delete is inside."""
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / ".done").write_text("a2m finished gamma\n", encoding="utf-8")
    (victim / "keep.txt").write_text("keep me\n", encoding="utf-8")
    victim_before = _tree_state(victim)

    calls: list[tuple[str, Path, Path]] = []
    depth = {"rmtree": 0}
    real = {
        "unlink": os.unlink,
        "rmdir": os.rmdir,
        "remove": os.remove,
        "replace": os.replace,
        "rename": os.rename,
        "rmtree": shutil.rmtree,
    }

    def recording(name: str):
        def wrapper(path, *args, **kwargs):
            if depth["rmtree"] == 0 and kwargs.get("dir_fd") is None:
                target = Path(os.fsdecode(path))
                calls.append((name, target, target.parent.resolve()))
            if name == "rmtree":
                depth["rmtree"] += 1
                try:
                    return real[name](path, *args, **kwargs)
                finally:
                    depth["rmtree"] -= 1
            return real[name](path, *args, **kwargs)

        return wrapper

    def run_recorded(argv: list[str]):
        with monkeypatch.context() as patch:
            for name in ("unlink", "rmdir", "remove", "replace", "rename"):
                patch.setattr(os, name, recording(name))
            patch.setattr(shutil, "rmtree", recording("rmtree"))
            return run_cli(argv, stages=[recorder])

    assert run_recorded(base_argv(mixed_exports, results_dir)).code == 0
    make_zip(mixed_exports, "alpha")
    shutil.rmtree(results_dir / "gamma")
    (results_dir / "gamma").symlink_to(victim)
    res = run_recorded(base_argv(mixed_exports, results_dir, "--force"))

    assert res.code == 0, res.err
    assert _tree_state(victim) == victim_before
    assert not (results_dir / "gamma").is_symlink()
    assert (results_dir / "gamma" / ".done").is_file()
    assert marker_names(results_dir) == {"beta", "gamma"}
    real_results = results_dir.resolve()
    assert any(name == "rmtree" for name, _, _ in calls), calls
    assert ("unlink", results_dir / "gamma") in [(name, path) for name, path, _ in calls], calls
    for name, path, parent in calls:
        assert parent == real_results or parent.is_relative_to(real_results), (name, path, parent)
        assert path.name not in ("", ".", ".."), (name, path)


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param("conflict-force", id="CP1-T45-conflict-under-force"),
        pytest.param("corrupt-resume", id="CP1-T45-corrupt-zip-under-resume"),
    ],
)
def test_CP1_T45_a_refused_proxy_keeps_no_stale_done_marker(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, make_zip, recorder, broken_zip_bytes, scenario: str
) -> None:
    """[CP1-T45] A proxy refused in this run loses its earlier output, so --resume never skips a changed bundle."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "gamma")
    if scenario == "conflict-force":
        make_bundle(exports, "alpha")
    else:
        make_zip(exports, "alpha")
    assert run_cli(base_argv(exports, results_dir), stages=[recorder]).code == 0
    assert marker_names(results_dir) == {"alpha", "gamma"}
    recorder.calls.clear()

    if scenario == "conflict-force":
        make_zip(exports, "alpha")
        res = run_cli(base_argv(exports, results_dir, "--force"), stages=[recorder])
    else:
        (exports / "alpha.zip").write_bytes(broken_zip_bytes)
        res = run_cli(base_argv(exports, results_dir, "--resume"), stages=[recorder])

    assert res.code == 0, res.err
    assert "alpha" not in marker_names(results_dir)
    assert not (results_dir / "alpha").exists()
    lines = log_lines(run_log(results_dir))
    assert any("removed earlier results of alpha" in line for line in lines), "\n".join(lines)
    assert "alpha" not in recorder.calls

    recorder.calls.clear()
    if scenario == "conflict-force":
        shutil.rmtree(exports / "alpha")
    else:
        make_zip(exports, "alpha")
    res = run_cli(base_argv(exports, results_dir, "--resume"), stages=[recorder])

    assert res.code == 0, res.err
    assert recorder.calls == ["alpha"]
    assert marker_names(results_dir) == {"alpha", "gamma"}


@needs_non_root
def test_CP1_T46_an_unreadable_earlier_result_fails_that_proxy_only_on_resume(
    mixed_exports: Path, results_dir: Path, run_cli, recorder
) -> None:
    """[CP1-T46] On --resume, a proxy whose earlier output cannot be read fails alone; the batch goes on."""
    assert run_cli(base_argv(mixed_exports, results_dir), stages=[recorder]).code == 0
    (results_dir / "gamma" / ".done").unlink()
    os.chmod(results_dir / "alpha", 0)
    recorder.calls.clear()
    try:
        res = run_cli(base_argv(mixed_exports, results_dir, "--resume"), stages=[recorder])
    finally:
        os.chmod(results_dir / "alpha", 0o700)

    assert res.code == 1, res.err
    assert_no_traceback(res.err)
    assert recorder.calls == ["gamma"]
    lines = log_lines(run_log(results_dir))
    assert any("failed alpha" in line and "ERROR" in line for line in lines), "\n".join(lines)
    assert "1 skipped" in res.out
    assert "1 failed" in res.out


# ---------------------------------------------------------------- adversarial round 03 additions
# New imports for the cases below live here so no existing line changes.

import threading  # noqa: E402

from a2m.cli import main as a2m_main  # noqa: E402
from a2m.discovery import check_zip_members  # noqa: E402
from a2m.engine import LlmChoice as A2mLlmChoice  # noqa: E402
from a2m.engine import RunOptions, prepare_run, run_batch  # noqa: E402
from a2m.errors import UnsafeBundleError, UsageError  # noqa: E402


def test_CP1_T47_a_second_run_on_a_results_folder_in_use_stops_and_changes_nothing(
    mixed_exports: Path, results_dir: Path, run_cli
) -> None:
    """[CP1-T47] While one run works on a results folder, a second run on it exits 2 and touches nothing."""
    started = threading.Event()
    release = threading.Event()
    outcome: dict[str, object] = {}

    def slow_stage(proxy) -> None:
        out_dir = Path(proxy.out_dir)
        (out_dir / "part1.txt").write_text("1\n", encoding="utf-8")
        if proxy.name == "alpha":
            started.set()
            assert release.wait(timeout=60), "test never released the first run"
        (out_dir / "part2.txt").write_text("2\n", encoding="utf-8")

    def first_run() -> None:
        try:
            outcome["code"] = a2m_main(base_argv(mixed_exports, results_dir), stages=[slow_stage])
        except BaseException as exc:  # noqa: BLE001  re-checked in the main thread, where pytest sees it
            outcome["error"] = exc

    def partial_then_crash(proxy) -> None:
        (Path(proxy.out_dir) / "b-partial.txt").write_text("b\n", encoding="utf-8")
        raise RuntimeError("second run stage")

    worker = threading.Thread(target=first_run)
    worker.start()
    try:
        assert started.wait(timeout=60), "first run never reached its stage"
        log_before = (results_dir / "run.log").read_bytes()
        res = run_cli(base_argv(mixed_exports, results_dir, "--resume"), stages=[partial_then_crash])
        log_after = (results_dir / "run.log").read_bytes()
    finally:
        release.set()
        worker.join(timeout=60)

    assert res.code == 2, (res.out, res.err)
    line = _one_stderr_line(res.err)
    assert "in use by another a2m run" in line, line
    assert_no_traceback(res.err, res.out)
    assert log_after == log_before, "the refused run wrote to run.log"

    assert "error" not in outcome, outcome
    assert outcome.get("code") == 0, outcome
    assert marker_names(results_dir) == ALL3
    for name in sorted(ALL3):
        assert sorted(p.name for p in (results_dir / name).iterdir()) == [".done", "part1.txt", "part2.txt"]


def test_CP1_T48_a_held_results_lock_refuses_force_and_leaves_results_byte_identical(
    mixed_exports: Path, results_dir: Path, run_cli, recorder
) -> None:
    """[CP1-T48] An outside holder of the results lock makes --force exit 2 with results unchanged."""
    assert run_cli(base_argv(mixed_exports, results_dir), stages=[recorder]).code == 0
    before = _tree_state(results_dir)
    recorder.calls.clear()

    with a2m_safefs.exclusive_lock(results_dir, a2m_layout.lock_path(results_dir)):
        res = run_cli(base_argv(mixed_exports, results_dir, "--force"), stages=[recorder])

    assert res.code == 2, (res.out, res.err)
    assert "in use by another a2m run" in _one_stderr_line(res.err)
    assert recorder.calls == []
    assert _tree_state(results_dir) == before

    # Once the holder lets go, the same command works: the lock never goes stale.
    res = run_cli(base_argv(mixed_exports, results_dir, "--force"), stages=[recorder])
    assert res.code == 0, res.err
    assert sorted(recorder.calls) == sorted(ALL3)


def test_CP1_T49_results_lock_is_reserved_and_never_followed_through_a_link(
    tmp_path: Path, mixed_exports: Path, results_dir: Path, run_cli, recorder
) -> None:
    """[CP1-T49] The lock file name is reserved and a link planted there is refused (exit 2), victim intact."""
    assert a2m_layout.LOCK_NAME in a2m_layout.RESERVED_NAMES
    assert a2m_layout.unsafe_name_reason(a2m_layout.LOCK_NAME.upper()) is not None

    assert run_cli(base_argv(mixed_exports, results_dir), stages=[recorder]).code == 0
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    lock = results_dir / a2m_layout.LOCK_NAME
    lock.unlink()
    lock.symlink_to(victim)
    before = _tree_state(results_dir)

    res = run_cli(base_argv(mixed_exports, results_dir, "--force"), stages=[recorder])

    assert res.code == 2, (res.out, res.err)
    assert a2m_layout.LOCK_NAME in _one_stderr_line(res.err)
    assert victim.read_text(encoding="utf-8") == "keep me\n"
    assert _tree_state(results_dir) == before


def test_CP1_T50_checks_are_repeated_once_the_lock_is_held(
    mixed_exports: Path, results_dir: Path, run_cli, recorder
) -> None:
    """[CP1-T50] A run whose checks passed before another run filled the folder stops instead of overwriting it."""
    options = RunOptions(
        input_dir=mixed_exports, out_dir=results_dir, llm=A2mLlmChoice.FAKE, no_runtime=True
    )
    plan = prepare_run(options)  # results folder does not exist yet: checks pass
    assert run_cli(base_argv(mixed_exports, results_dir), stages=[recorder]).code == 0
    before = _tree_state(results_dir)
    recorder.calls.clear()

    with pytest.raises(UsageError, match="--resume"):
        run_batch(plan, [recorder])

    assert recorder.calls == []
    assert _tree_state(results_dir) == before


@pytest.mark.parametrize(
    "twin",
    [
        pytest.param("corrupt-zip", id="CP1-T51-folder-and-corrupt-zip"),
        pytest.param("zip-slip", id="CP1-T51-folder-and-zip-slip-zip"),
        pytest.param("corrupt-zip-other-case", id="CP1-T51-folder-and-corrupt-zip-differing-in-case"),
        pytest.param("locked-folder", id="CP1-T51-unreadable-folder-and-valid-zip"),
    ],
)
def test_CP1_T51_a_name_shared_with_a_refused_item_is_a_conflict(
    tmp_path: Path,
    results_dir: Path,
    run_cli,
    make_bundle,
    make_zip,
    recorder,
    broken_zip_bytes,
    evil_dotdot_zip_member,
    twin: str,
) -> None:
    """[CP1-T51] A proxy name shared with an item refused while reading it is a conflict: neither is processed."""
    if twin == "locked-folder" and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("file permissions do not apply to root")
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "gamma")
    folder = make_bundle(exports, "ALPHA" if twin == "corrupt-zip-other-case" else "alpha")
    if twin in ("corrupt-zip", "corrupt-zip-other-case"):
        (exports / "alpha.zip").write_bytes(broken_zip_bytes)
    elif twin == "zip-slip":
        make_zip(exports, "alpha", extra_members=[evil_dotdot_zip_member])
    else:
        make_zip(exports, "alpha")
        os.chmod(folder, 0)
    try:
        res = run_cli(base_argv(exports, results_dir), stages=[recorder])
    finally:
        os.chmod(folder, 0o700)

    assert res.code == 0, res.err
    assert_no_traceback(res.err, res.out)
    assert recorder.calls == ["gamma"]
    assert marker_names(results_dir) == {"gamma"}
    assert not (results_dir / "alpha").exists()
    assert not (results_dir / "ALPHA").exists()
    assert "0 done" not in res.out and "1 done" in res.out
    assert "2 refused" in res.out
    lines = log_lines(run_log(results_dir))
    for item in (folder.name, "alpha.zip"):
        assert any(
            "ERROR" in line and "refused" in line and f"({item})" in line and "name conflict" in line
            for line in lines
        ), (item, "\n".join(lines))
    assert not any("processing alpha" in line.lower() or "done alpha" in line.lower() for line in lines)


def test_CP1_T52_a_zip_whose_members_start_with_dot_slash_is_a_proxy(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, recorder
) -> None:
    """[CP1-T52] A zip written as './apiproxy/...' is found, unpacked and finished like any other."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "gamma")
    _write_raw_zip(
        exports / "dot.zip",
        [("./apiproxy/dot.xml", "<APIProxy name=\"dot\"/>\n"), ("./apiproxy/policies/q.xml", "<Quota/>\n")],
    )
    seen: dict[str, bool] = {}

    def check_unpacked(proxy) -> None:
        if proxy.name == "dot":
            seen["dot"] = (Path(proxy.bundle_dir) / "apiproxy" / "policies" / "q.xml").is_file()

    res = run_cli(base_argv(exports, results_dir), stages=[check_unpacked, recorder])

    assert res.code == 0, res.err
    assert sorted(recorder.calls) == ["dot", "gamma"]
    assert seen == {"dot": True}
    assert marker_names(results_dir) == {"dot", "gamma"}
    assert "0 refused" in res.out
    assert any("found proxy dot" in line for line in log_lines(run_log(results_dir)))


@pytest.mark.parametrize(
    "members",
    [
        pytest.param(["apiproxy/A.xml", "apiproxy/a.xml"], id="CP1-T53-files-differing-in-case"),
        pytest.param(["apiproxy/A.xml", "apiproxy/a.xml/x.xml"], id="CP1-T53-file-and-folder-differing-in-case"),
        pytest.param(
            ["apiproxy/Policies/x.xml", "apiproxy/policies/y.xml"], id="CP1-T53-folders-differing-in-case"
        ),
        pytest.param(["apiproxy/bad.xml", "APIPROXY/bad.xml"], id="CP1-T53-root-folders-differing-in-case"),
    ],
)
def test_CP1_T53_zip_members_differing_only_in_case_are_refused_on_every_platform(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, recorder, members: list[str]
) -> None:
    """[CP1-T53] Members that would overwrite each other on a case-insensitive disk make the zip refused."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "alpha")
    _write_raw_zip(exports / "bad.zip", [("apiproxy/bad-root.xml", "<APIProxy/>\n")] + [(m, m) for m in members])

    res = run_cli(base_argv(exports, results_dir), stages=[recorder])

    assert res.code == 0, res.err
    assert_no_traceback(res.err, res.out)
    assert recorder.calls == ["alpha"]
    assert marker_names(results_dir) == {"alpha"}
    assert "1 refused" in res.out and "0 failed" in res.out
    lines = log_lines(run_log(results_dir))
    assert any("refused bad" in line and "letter case" in line for line in lines), "\n".join(lines)

    with zipfile.ZipFile(exports / "bad.zip") as zf:
        infos = zf.infolist()
    with pytest.raises(UnsafeBundleError, match="letter case"):
        check_zip_members(infos)


def test_CP1_T54_ctrl_c_is_one_line_exit_130_and_logged(mixed_exports: Path, results_dir: Path, run_cli, recorder) -> None:
    """[CP1-T54] Ctrl-C mid-run: exit 130, one stderr line, run.log says so, no .done for that proxy; resume works."""

    def interrupt_in_beta(proxy) -> None:
        if proxy.name == "beta":
            raise KeyboardInterrupt

    try:
        res = run_cli(base_argv(mixed_exports, results_dir), stages=[recorder, interrupt_in_beta])
    except KeyboardInterrupt:
        pytest.fail("KeyboardInterrupt escaped a2m.cli.main")

    assert res.code == 130, (res.out, res.err)
    line = _one_stderr_line(res.err)
    assert "interrupted" in line and "--resume" in line, line
    assert_no_traceback(res.err, res.out)
    assert recorder.calls == ["alpha", "beta"]
    assert marker_names(results_dir) == {"alpha"}
    assert not (results_dir / ".a2m-work" / "beta").exists()
    text = run_log(results_dir)
    assert any(
        "ERROR" in entry and "run interrupted while processing beta" in entry and "--resume" in entry
        for entry in log_lines(text)
    ), text

    recorder.calls.clear()
    res = run_cli(base_argv(mixed_exports, results_dir, "--resume"), stages=[recorder])
    assert res.code == 0, res.err
    assert recorder.calls == ["beta", "gamma"]
    assert marker_names(results_dir) == ALL3


_VENV_BIN = Path(sys.executable).parent
_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "tool",
    [
        pytest.param(["ruff", "check", "a2m", "tests"], id="CP1-T55-ruff"),
        pytest.param(["mypy", "a2m"], id="CP1-T55-mypy"),
    ],
)
def test_CP1_T55_static_checks_are_clean(tool: list[str], subprocess_env: dict[str, str]) -> None:
    """[CP1-T55] ruff and mypy report nothing on the package (skipped when the tool is not installed)."""
    exe = _VENV_BIN / tool[0]
    if not exe.is_file():
        pytest.skip(f"{tool[0]} is not installed in the venv")
    proc = subprocess.run(
        [str(exe), *tool[1:]],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        env=subprocess_env,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---------------------------------------------------------------- adversarial round 04 additions
# New imports for the cases below live here so no existing line changes.

import stat  # noqa: E402
import tomllib  # noqa: E402
import unicodedata  # noqa: E402

from a2m.errors import NotPlainFileError  # noqa: E402
from a2m.layout import collision_key  # noqa: E402

_needs_mkfifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="os.mkfifo is not available")


def _kind_state(folder: Path) -> dict[str, object]:
    """Like _tree_state, but never opens FIFOs or other special files (that could block)."""
    state: dict[str, object] = {}
    for path in sorted(folder.rglob("*")):
        rel = str(path.relative_to(folder))
        mode = os.lstat(path).st_mode
        if stat.S_ISLNK(mode):
            state[rel] = ("link", os.readlink(path))
        elif stat.S_ISDIR(mode):
            state[rel] = ("dir",)
        elif stat.S_ISREG(mode):
            state[rel] = ("file", path.read_bytes())
        else:
            state[rel] = ("special", stat.S_IFMT(mode))
    return state


def _in_thread(func, timeout: float = 10.0):
    """Run ``func`` in a daemon thread; fail (instead of hanging the suite) if it does not return in time."""
    box: dict[str, object] = {}

    def target() -> None:
        try:
            box["value"] = func()
        except BaseException as exc:  # noqa: BLE001  handed back to the test below
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        pytest.fail(f"call did not return within {timeout} seconds (blocked on a special file?)")
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box.get("value")


@_needs_mkfifo
@pytest.mark.parametrize(
    ("entry", "flag"),
    [
        pytest.param("run.log", "--force", id="CP1-T56-fifo-at-run-log"),
        pytest.param(".a2m-lock", "--force", id="CP1-T56-fifo-at-lock"),
        pytest.param(".a2m-results", "--force", id="CP1-T56-fifo-at-results-marker"),
        pytest.param(".a2m-work", "--force", id="CP1-T56-fifo-at-work-root"),
        pytest.param("alpha/.done", "--resume", id="CP1-T56-fifo-at-done-marker-resume"),
        pytest.param("alpha/.done", "--force", id="CP1-T56-fifo-at-done-marker-force"),
    ],
)
def test_CP1_T56_a_fifo_at_any_a2m_entry_is_a_usage_error_not_a_hang(
    mixed_exports: Path, results_dir: Path, run_cli, recorder, subprocess_env: dict[str, str], entry: str, flag: str
) -> None:
    """[CP1-T56] A FIFO at run.log, the lock, the marker, the work root or a .done: exit 2 at once, nothing changed."""
    assert run_cli(base_argv(mixed_exports, results_dir), stages=[recorder]).code == 0
    target = results_dir / entry
    if os.path.lexists(target):
        target.unlink()
    os.mkfifo(target)
    before = _kind_state(results_dir)

    try:
        proc = subprocess.run(
            [str(_VENV_BIN / "a2m"), *base_argv(mixed_exports, results_dir, flag)],
            capture_output=True,
            text=True,
            cwd=results_dir.parent,
            env=subprocess_env,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"a2m hung on a FIFO at {entry}")

    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    line = _one_stderr_line(proc.stderr)
    assert entry in line and ("plain file" in line or "folder" in line), line
    assert_no_traceback(proc.stderr, proc.stdout)
    assert _kind_state(results_dir) == before


@_needs_mkfifo
def test_CP1_T57_files_a2m_opens_are_opened_only_when_plain_even_after_the_checks(
    tmp_path: Path, mixed_exports: Path, results_dir: Path, run_cli, recorder
) -> None:
    """[CP1-T57] The guarded opener refuses a FIFO, folder or link without blocking; run_batch maps it to exit 2."""
    root = tmp_path / "root"
    root.mkdir()
    os.mkfifo(root / "fifo")
    (root / "folder").mkdir()
    (root / "plain").write_text("x\n", encoding="utf-8")
    (root / "link").symlink_to(root / "plain")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    for name in ("fifo", "folder", "link"):
        with pytest.raises(NotPlainFileError):
            _in_thread(lambda name=name: a2m_safefs.open_plain_file(root, root / name, flags))
    fd = a2m_safefs.open_plain_file(root, root / "plain", flags)
    os.close(fd)
    assert (root / "plain").read_text(encoding="utf-8") == "x\n"

    # A FIFO that appears at run.log after prepare_run checked the folder.
    assert run_cli(base_argv(mixed_exports, results_dir), stages=[recorder]).code == 0
    options = RunOptions(
        input_dir=mixed_exports, out_dir=results_dir, force=True, llm=A2mLlmChoice.FAKE, no_runtime=True
    )
    plan = prepare_run(options)
    (results_dir / "run.log").unlink()
    os.mkfifo(results_dir / "run.log")
    recorder.calls.clear()
    with pytest.raises(UsageError, match="run.log"):
        _in_thread(lambda: run_batch(plan, [recorder]))
    assert recorder.calls == []


@pytest.mark.parametrize(
    "members",
    [
        pytest.param(
            ["apiproxy/policies/café.xml", "apiproxy/policies/café.xml"],
            id="CP1-T58-files-differing-in-unicode-normalization",
        ),
        pytest.param(
            ["apiproxy/café/x.xml", "apiproxy/café/y.xml"],
            id="CP1-T58-folders-differing-in-unicode-normalization",
        ),
        pytest.param(["apiproxy/x.xml", "apiproxy/x.xml."], id="CP1-T58-files-differing-in-a-trailing-dot"),
        pytest.param(["apiproxy/x.xml", "apiproxy/x.xml "], id="CP1-T58-files-differing-in-a-trailing-space"),
        pytest.param(["apiproxy/p/x.xml", "apiproxy/p./y.xml"], id="CP1-T58-folders-differing-in-a-trailing-dot"),
        pytest.param(["apiproxy/X.xml", "apiproxy/x.xml.."], id="CP1-T58-case-and-trailing-dots"),
    ],
)
def test_CP1_T58_zip_members_landing_on_one_file_anywhere_are_refused(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, recorder, members: list[str]
) -> None:
    """[CP1-T58] Members that one file system (macOS NFC/NFD, Windows trailing dots) merges make the zip refused."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "alpha")
    _write_raw_zip(exports / "bad.zip", [("apiproxy/bad-root.xml", "<APIProxy/>\n")] + [(m, m) for m in members])

    res = run_cli(base_argv(exports, results_dir), stages=[recorder])

    assert res.code == 0, res.err
    assert_no_traceback(res.err, res.out)
    assert recorder.calls == ["alpha"]
    assert marker_names(results_dir) == {"alpha"}
    assert "1 refused" in res.out and "0 failed" in res.out
    lines = log_lines(run_log(results_dir))
    assert any("refused bad" in line and "overwrite" in line for line in lines), "\n".join(lines)

    with zipfile.ZipFile(exports / "bad.zip") as zf:
        infos = zf.infolist()
    with pytest.raises(UnsafeBundleError, match="overwrite"):
        check_zip_members(infos)


@pytest.mark.parametrize(
    ("folder", "zip_stem"),
    [
        pytest.param("alpha", "alpha.", id="CP1-T59-folder-and-zip-with-a-trailing-dot"),
        pytest.param("alpha ", "alpha", id="CP1-T59-folder-with-a-trailing-space-and-zip"),
        pytest.param("café", "café", id="CP1-T59-folder-and-zip-differing-in-unicode-normalization"),
        pytest.param("Café.", "café", id="CP1-T59-case-normalization-and-trailing-dot-together"),
    ],
)
def test_CP1_T59_proxy_names_landing_on_one_folder_anywhere_are_both_refused(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, make_zip, recorder, folder: str, zip_stem: str
) -> None:
    """[CP1-T59] alpha/ and 'alpha..zip', or NFC and NFD names, share one results folder somewhere: both refused."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "gamma")
    make_bundle(exports, folder)
    zip_path = make_zip(exports, zip_stem)
    if len(list(exports.iterdir())) != 3:
        pytest.skip("this file system already merges the two names")

    res = run_cli(base_argv(exports, results_dir), stages=[recorder])

    assert res.code == 0, res.err
    assert_no_traceback(res.err, res.out)
    assert recorder.calls == ["gamma"]
    assert marker_names(results_dir) == {"gamma"}
    assert sorted(p.name for p in results_dir.iterdir() if not p.name.startswith(".")) == ["gamma", "run.log"]
    assert "2 refused" in res.out and "0 failed" in res.out
    lines = log_lines(run_log(results_dir))
    for item in (folder, zip_path.name):
        assert any(
            "ERROR" in line and "refused" in line and f"({item})" in line and "name conflict" in line
            for line in lines
        ), (item, "\n".join(lines))


def test_CP1_T60_every_name_collision_check_uses_the_one_collision_key() -> None:
    """[CP1-T60] Structural: only a2m/layout.py folds names (casefold, normalize); every other check calls it."""
    import a2m

    assert collision_key("Café. ") == collision_key("café") == collision_key("CAFÉ..")
    assert collision_key("run.log.") == collision_key("RUN.LOG")
    assert collision_key("alpha") != collision_key("alpha2")
    assert unicodedata.is_normalized("NFC", collision_key("café"))

    package = Path(a2m.__file__).parent
    offenders: list[str] = []
    callers: list[str] = []
    for source in sorted(package.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name == "collision_key":
                callers.append(source.name)
            if source.name != "layout.py" and name in ("casefold", "lower", "upper", "normalize"):
                if name == "lower" and isinstance(func, ast.Attribute):
                    # str.lower() on a file suffix (".ZIP") is not a name-collision check.
                    inner = func.value
                    if isinstance(inner, ast.Attribute) and inner.attr == "suffix":
                        continue
                offenders.append(f"{source.name}:{node.lineno} calls {name}()")
    assert offenders == [], "name folding outside a2m/layout.py:\n" + "\n".join(offenders)
    assert {"discovery.py", "engine.py", "layout.py"} <= set(callers), callers


@pytest.mark.parametrize(
    ("member", "shown"),
    [
        pytest.param("apiproxy/" + "x" * 300 + ".xml", "x" * 50, id="CP1-T61-name-part-over-255-characters"),
        pytest.param("apiproxy/" + "é" * 150 + ".xml", "é" * 50, id="CP1-T61-name-part-over-255-bytes"),
        pytest.param("apiproxy/" + "d/" * 70 + "deep.xml", "deep.xml", id="CP1-T61-member-over-64-folders-deep"),
        pytest.param(
            "apiproxy/" + "/".join(["f" * 19] * 55) + "/long.xml", "f" * 19, id="CP1-T61-member-over-1024-characters"
        ),
    ],
)
def test_CP1_T61_member_paths_too_long_for_any_file_system_make_the_zip_refused(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, make_zip, recorder, member: str, shown: str
) -> None:
    """[CP1-T61] A member path no file system accepts is refused in discovery (exit 0), never a crash."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "alpha")
    make_zip(exports, "beta", extra_members=[(member, "<x/>\n")])

    res = run_cli(base_argv(exports, results_dir), stages=[recorder])

    assert res.code == 0, (res.out, res.err)
    assert_no_traceback(res.err, res.out)
    assert recorder.calls == ["alpha"]
    assert "1 refused" in res.out and "0 failed" in res.out
    text = run_log(results_dir)
    assert "Traceback" not in text
    lines = log_lines(text)
    assert any("ERROR" in line and "refused beta" in line and shown in line for line in lines), "\n".join(lines)
    assert not any("processing beta" in line for line in lines)

    with zipfile.ZipFile(exports / "beta.zip") as zf:
        infos = zf.infolist()
    with pytest.raises(UnsafeBundleError):
        check_zip_members(infos)


def test_CP1_T62_a_name_too_long_while_unpacking_is_a_refusal_not_a_failure(
    mixed_exports: Path, results_dir: Path, run_cli, recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP1-T62] ENAMETOOLONG while unpacking a zip comes from its member names: refused (exit 0), not failed."""
    real_open = Path.open

    def too_long_open(self: Path, mode: str = "r", *args, **kwargs):
        if "w" in mode and a2m_layout.WORK_DIR_NAME in self.parts:
            raise OSError(errno.ENAMETOOLONG, "File name too long", str(self))
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", too_long_open)

    res = run_cli(base_argv(mixed_exports, results_dir), stages=[recorder])

    assert res.code == 0, res.err
    assert_no_traceback(res.err, res.out)
    lines = log_lines(run_log(results_dir))
    assert any("refused beta" in line and "too long" in line for line in lines), "\n".join(lines)
    assert not any("failed beta" in line for line in lines)
    assert sorted(recorder.calls) == ["alpha", "gamma"]
    assert marker_names(results_dir) == {"alpha", "gamma"}
    assert not (results_dir / ".a2m-work").exists()
    assert "1 refused" in res.out and "0 failed" in res.out


def _repo_cache_state() -> dict[str, object]:
    state: dict[str, object] = {}
    for name in (".mypy_cache", ".ruff_cache"):
        path = _REPO_ROOT / name
        state[name] = sorted((str(p.relative_to(path)), p.stat().st_mtime_ns) for p in path.rglob("*")) if (
            path.exists()
        ) else None
    return state


def test_CP1_T63_static_checks_run_by_tests_write_no_cache_into_the_repo(subprocess_env: dict[str, str]) -> None:
    """[CP1-T63] The env tests hand to ruff and mypy points their caches under pytest's tmp folder."""
    for var in ("MYPY_CACHE_DIR", "RUFF_CACHE_DIR"):
        assert var in subprocess_env, var
        cache = Path(subprocess_env[var]).resolve()
        assert not cache.is_relative_to(_REPO_ROOT), (var, cache)
    before = _repo_cache_state()
    for tool in (["ruff", "check", "a2m", "tests"], ["mypy", "a2m"]):
        exe = _VENV_BIN / tool[0]
        if not exe.is_file():
            continue  # CP1-T64 fails when a dev tool is missing
        subprocess.run(
            [str(exe), *tool[1:]], capture_output=True, cwd=_REPO_ROOT, env=subprocess_env, timeout=300, check=False
        )
    assert _repo_cache_state() == before


@pytest.mark.parametrize(
    "tool",
    [
        pytest.param(["ruff", "check", "--cache-dir", "{cache}", "a2m", "tests"], id="CP1-T64-ruff"),
        pytest.param(["mypy", "--cache-dir", "{cache}", "a2m"], id="CP1-T64-mypy"),
    ],
)
def test_CP1_T64_static_checks_are_dev_dependencies_and_never_pass_by_skipping(
    tmp_path: Path, subprocess_env: dict[str, str], tool: list[str]
) -> None:
    """[CP1-T64] ruff and mypy are in the dev extra; missing tools fail (unless A2M_ALLOW_MISSING_STATIC_CHECKS=1)."""
    project = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    dev = project["optional-dependencies"]["dev"]
    assert any(re.fullmatch(rf"{tool[0]}\s*>=\s*[\d.]+", spec) for spec in dev), dev

    exe = _VENV_BIN / tool[0]
    if not exe.is_file():
        if os.environ.get("A2M_ALLOW_MISSING_STATIC_CHECKS") == "1":
            pytest.skip(f"{tool[0]} is missing and A2M_ALLOW_MISSING_STATIC_CHECKS=1")
        pytest.fail(f"{tool[0]} is not installed in the venv; run: pip install -e '.[dev]'")
    args = [arg.replace("{cache}", str(tmp_path / f"{tool[0]}-cache")) for arg in tool[1:]]
    proc = subprocess.run(
        [str(exe), *args], capture_output=True, text=True, cwd=_REPO_ROOT, env=subprocess_env, timeout=300, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (tmp_path / f"{tool[0]}-cache").is_dir()


# ---------------------------------------------------------------- adversarial round 05 additions
# New imports for the cases below live here so no existing line changes.

import io  # noqa: E402
from collections import Counter  # noqa: E402

from a2m.discovery import member_is_dir  # noqa: E402

_A2M_SOURCES = sorted((_REPO_ROOT / "a2m").glob("*.py"))
_SUMMARY_RE = re.compile(r"^a2m: (\d+) done, (\d+) skipped as already done, (\d+) refused, (\d+) failed\. Log: ")


def _strict_stream() -> io.TextIOWrapper:
    """A text stream like a real UTF-8 terminal stdout: errors='strict', so a lone surrogate raises."""
    return io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict", newline="\n")


def _stream_text(stream: io.TextIOWrapper) -> str:
    stream.flush()
    raw = stream.buffer.getvalue()  # type: ignore[attr-defined]
    return raw.decode("utf-8")


def _run_strict(monkeypatch: pytest.MonkeyPatch, argv: list[str], stages=None) -> tuple[int, str, str]:
    out, err = _strict_stream(), _strict_stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    try:
        code = a2m_main(argv, stages=stages)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
    finally:
        monkeypatch.undo()
    return code, _stream_text(out), _stream_text(err)


def _assert_safe_line(line: str) -> None:
    assert line.isprintable(), f"unescaped control or undecodable character in {line!r}"


@pytest.mark.parametrize(
    "out_name",
    [
        pytest.param("res\nx", id="CP1-T65-newline"),
        pytest.param("res\x1b[31mred", id="CP1-T65-escape-sequence"),
        pytest.param("res\udcff", id="CP1-T65-undecodable-byte"),
        pytest.param("res x", id="CP1-T65-line-separator"),
    ],
)
def test_CP1_T65_the_summary_line_is_one_safe_line_whatever_the_out_path(
    tmp_path: Path, mixed_exports: Path, recorder, monkeypatch: pytest.MonkeyPatch, out_name: str
) -> None:
    """[CP1-T65] --out with a newline, escape or undecodable byte: exit 0, exactly one escaped stdout line."""
    try:
        os.fsencode(out_name)
    except UnicodeEncodeError:
        pytest.skip("this platform cannot name a file with that character")
    results = tmp_path / out_name

    code, out, err = _run_strict(monkeypatch, base_argv(mixed_exports, results), stages=[recorder])

    assert code == 0, (out, err)
    assert_no_traceback(out, err)
    assert err == ""
    lines = out.splitlines()
    assert len(lines) == 1, out
    _assert_safe_line(lines[0])
    assert _SUMMARY_RE.match(lines[0]), lines[0]
    assert "3 done" in lines[0] and "0 failed" in lines[0]
    assert marker_names(results) == ALL3


@pytest.mark.parametrize(
    "case",
    [
        pytest.param("missing-input", id="CP1-T65-usage-error-missing-input"),
        pytest.param("unrecognized-argument", id="CP1-T65-usage-error-unrecognized-argument"),
        pytest.param("bad-number", id="CP1-T65-usage-error-bad-number"),
    ],
)
def test_CP1_T65_usage_errors_are_one_safe_stderr_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """[CP1-T65] Usage errors echoing user text (paths, stray arguments) are one escaped stderr line, exit 2."""
    nasty = "x\x1b]0;owned\x07\nINFO forged\udcff"
    results = tmp_path / "results"
    if case == "missing-input":
        argv = base_argv(tmp_path / nasty, results)
    elif case == "unrecognized-argument":
        argv = [*base_argv(tmp_path, results), nasty]
    else:
        argv = [*base_argv(tmp_path, results), "--max-fix-attempts", nasty]

    code, out, err = _run_strict(monkeypatch, argv)

    assert code == 2, (out, err)
    assert out == ""
    assert_no_traceback(err)
    lines = err.splitlines()
    assert len(lines) == 1, err
    _assert_safe_line(lines[0])
    assert "\x1b" not in err and "\x07" not in err
    assert not results.exists()


@pytest.mark.skipif(os.name != "posix", reason="argv bytes that are not UTF-8 only exist on POSIX")
@pytest.mark.parametrize(
    "out_bytes",
    [
        pytest.param(b"res\xff", id="CP1-T66-undecodable-byte"),
        pytest.param(b"res\nx", id="CP1-T66-newline"),
    ],
)
def test_CP1_T66_console_script_prints_one_line_for_an_undecodable_out_path(
    tmp_path: Path, subprocess_env: dict[str, str], out_bytes: bytes
) -> None:
    """[CP1-T66] The installed a2m with a strict UTF-8 stdout and a non-UTF-8 --out: exit 0, one stdout line."""
    exports = tmp_path / "ex"
    exports.mkdir()
    (exports / "alpha" / "apiproxy").mkdir(parents=True)
    (exports / "alpha" / "apiproxy" / "alpha.xml").write_text('<APIProxy name="alpha"/>\n', encoding="utf-8")
    out_dir = os.fsencode(tmp_path) + b"/" + out_bytes
    env = dict(subprocess_env)
    env["PYTHONIOENCODING"] = "utf-8:strict"
    env["LC_ALL"] = "C.UTF-8"

    proc = subprocess.run(
        [os.fsencode(_VENV_BIN / "a2m"), b"migrate", os.fsencode(exports), b"--out", out_dir, b"--llm", b"fake",
         b"--no-runtime"],
        capture_output=True,
        cwd=tmp_path,
        env=env,
        timeout=60,
        check=False,
    )

    out = proc.stdout.decode("utf-8")
    err = proc.stderr.decode("utf-8", "backslashreplace")
    assert proc.returncode == 0, (out, err)
    assert_no_traceback(out, err)
    lines = out.splitlines()
    assert len(lines) == 1, out
    _assert_safe_line(lines[0])
    assert "1 done" in lines[0] and "0 failed" in lines[0]
    # CP9 layout: the proxy's folder is results/<bucket>/alpha/, in exactly one bucket (tests/e2e_support.py).
    homes = [b for b in (b"verified", b"needs-review", b"unsupported") if os.path.isdir(out_dir + b"/" + b + b"/alpha")]
    assert len(homes) == 1, f"alpha must sit in exactly one of results/<bucket>/alpha, found in {homes!r}"
    assert os.path.isfile(out_dir + b"/" + homes[0] + b"/alpha/.done")


def _is_sys_stream(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr in {"stdout", "stderr", "__stdout__", "__stderr__"}
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def _enclosing_functions(tree: ast.AST) -> dict[ast.AST, str]:
    """Map every node to the name of the innermost function that contains it ('' at module level)."""
    owner: dict[ast.AST, str] = {}

    def visit(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else current
            owner[child] = name
            visit(child, name)

    visit(tree, "")
    return owner


def test_CP1_T67_no_terminal_output_bypasses_the_one_line_formatter() -> None:
    """[CP1-T67] Structural: only cli._write writes to the terminal, and it escapes every line with one_line."""
    problems: list[str] = []
    for path in _A2M_SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner = _enclosing_functions(tree)
        is_cli = path.name == "cli.py"
        for node in ast.walk(tree):
            where = f"{path.name}:{getattr(node, 'lineno', '?')}"
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"print", "input"}:
                problems.append(f"{where} calls {node.func.id}()")
            if isinstance(node, ast.Attribute) and node.attr in {"StreamHandler", "basicConfig", "excepthook"}:
                problems.append(f"{where} uses {node.attr}")
            if isinstance(node, ast.Attribute) and node.attr == "write" and (
                isinstance(node.value, ast.Name) and node.value.id == "os"
            ):
                problems.append(f"{where} uses os.write")
            if _is_sys_stream(node) and not (is_cli and owner.get(node) in {"_say", "_print_message"}):
                problems.append(f"{where} touches sys.{node.attr} outside cli._say/_print_message")
            if (
                is_cli
                and isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"write", "writelines"}
                and owner.get(node) != "_write"
            ):
                problems.append(f"{where} writes to a stream outside cli._write")
    assert problems == [], "\n".join(problems)

    cli_tree = ast.parse((_REPO_ROOT / "a2m" / "cli.py").read_text(encoding="utf-8"))
    funcs = {n.name: n for n in ast.walk(cli_tree) if isinstance(n, ast.FunctionDef)}
    for name in ("_write", "_say", "_print_message"):
        assert name in funcs, f"cli.{name} is missing"
    calls_in = {
        name: {c.func.id for c in ast.walk(funcs[name]) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        for name in ("_write", "_say", "_print_message")
    }
    assert "one_line" in calls_in["_write"]
    assert "_write" in calls_in["_say"] and "_write" in calls_in["_print_message"]


def test_CP1_T68_run_log_traceback_lines_are_escaped_too(
    mixed_exports: Path, results_dir: Path, run_cli
) -> None:
    """[CP1-T68] A crash whose message holds control characters: every run.log line, traceback included, is safe."""

    def nasty_stage(proxy) -> None:
        if proxy.name == "beta":
            raise RuntimeError("bad\x1b[31m value\x07 from \udcff input\nsecond line\tend")

    res = run_cli(base_argv(mixed_exports, results_dir), stages=[nasty_stage])

    assert res.code == 1, res.err
    raw = (results_dir / "run.log").read_bytes().decode("utf-8")
    lines = raw.split("\n")
    assert lines[-1] == ""
    assert any("Traceback (most recent call last)" in line for line in lines)
    for line in lines[:-1]:
        assert LOG_PREFIX_RE.match(line), f"line without its own prefix: {line!r}"
        _assert_safe_line(line)
    _assert_safe_line(res.out.rstrip("\n"))


def _assert_one_outcome_each(plan, result) -> None:
    """Each selected input item is in exactly one outcome list; the counts add up to the items."""
    expected = Counter([s.name for s in plan.selected] + [r.name for r in plan.selected_rejected])
    got = Counter([*result.finished, *result.skipped, *result.refused, *result.crashed])
    assert got == expected, (result, expected)
    lists = (result.finished, result.skipped, result.refused, result.crashed)
    for name, count in expected.items():
        holders = [i for i, outcome in enumerate(lists) if name in outcome]
        if count == 1:
            assert len(holders) == 1, (name, result)


@needs_non_root
def test_CP1_T69_a_refused_proxy_whose_old_results_cannot_be_removed_has_one_outcome(
    tmp_path: Path, results_dir: Path, run_cli, recorder, make_bundle, make_zip, broken_zip_bytes: bytes
) -> None:
    """[CP1-T69] Refused bundle + undeletable old results: counted once, as failed (exit 1), never also refused."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "g")
    make_zip(exports, "alpha")
    assert run_cli(base_argv(exports, results_dir), stages=[recorder]).code == 0
    (exports / "alpha.zip").write_bytes(broken_zip_bytes)
    os.chmod(results_dir / "alpha", 0)
    try:
        res = run_cli(base_argv(exports, results_dir, "--resume"), stages=[recorder])
        plan = prepare_run(
            RunOptions(input_dir=exports, out_dir=results_dir, resume=True, llm=A2mLlmChoice.FAKE, no_runtime=True)
        )
        result = run_batch(plan, [recorder])
    finally:
        os.chmod(results_dir / "alpha", 0o700)

    assert res.code == 1, res.err
    assert_no_traceback(res.err, res.out)
    match = _SUMMARY_RE.match(res.out)
    assert match, res.out
    assert [int(n) for n in match.groups()] == [0, 1, 0, 1], res.out
    assert sum(int(n) for n in match.groups()) == 2
    assert result.crashed == ["alpha"] and result.refused == [] and result.skipped == ["g"]
    _assert_one_outcome_each(plan, result)
    lines = log_lines(run_log(results_dir))
    assert any("failed alpha" in line and "could not remove" in line for line in lines), "\n".join(lines)


def _t70_setup(case: str, exports: Path, results: Path, make_bundle, make_zip, broken: bytes) -> dict[str, object]:
    """Build the exports for one CP1-T70 scenario; return extra RunOptions fields and the stages to use."""
    make_bundle(exports, "alpha")
    make_zip(exports, "beta")
    make_bundle(exports, "gamma")
    opts: dict[str, object] = {}

    def crash_beta(proxy) -> None:
        if proxy.name == "beta":
            raise RuntimeError("boom")

    stages: list = [lambda proxy: None]
    if case == "crash":
        stages = [crash_beta]
    elif case == "refused-and-conflict":
        (exports / "delta.zip").write_bytes(broken)
        make_zip(exports, "alpha")  # alpha/ and alpha.zip: a name conflict, both refused
        _write_raw_zip(exports / "eps.zip", [("apiproxy/../x.xml", "x")])
    elif case == "resume":
        run_batch(prepare_run(RunOptions(input_dir=exports, out_dir=results, llm=A2mLlmChoice.FAKE)), stages)
        (results / "gamma" / ".done").unlink()
        opts["resume"] = True
    elif case == "only":
        (exports / "delta.zip").write_bytes(broken)
        opts["only"] = "delta"
    elif case == "refused-while-unpacking":
        _write_raw_zip(exports / "zed.zip", [("apiproxy/zed.xml", "<APIProxy/>\n")])
        opts["_break_zip"] = True  # the test makes unpacking zed.zip raise UnsafeBundleError
    return {"opts": opts, "stages": stages}


@pytest.mark.parametrize(
    "case",
    [
        pytest.param("plain", id="CP1-T70-plain"),
        pytest.param("crash", id="CP1-T70-one-proxy-crashes"),
        pytest.param("refused-and-conflict", id="CP1-T70-refused-and-name-conflict"),
        pytest.param("resume", id="CP1-T70-resume-skips"),
        pytest.param("only", id="CP1-T70-only-a-refused-item"),
        pytest.param("refused-while-unpacking", id="CP1-T70-refused-while-unpacking"),
    ],
)
def test_CP1_T70_every_selected_item_lands_in_exactly_one_outcome(
    tmp_path: Path, make_bundle, make_zip, broken_zip_bytes: bytes, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """[CP1-T70] Structural across paths: finished + skipped + refused + failed == selected items, each once."""
    exports = tmp_path / "exports"
    exports.mkdir()
    results = tmp_path / "results"
    setup = _t70_setup(case, exports, results, make_bundle, make_zip, broken_zip_bytes)
    opts = dict(setup["opts"])  # type: ignore[call-overload]
    if opts.pop("_break_zip", False):
        import a2m.engine as a2m_engine

        def refusing_extract(zip_path: Path, dest: Path) -> None:
            if zip_path.name == "zed.zip":
                raise UnsafeBundleError("simulated refusal while unpacking")

        monkeypatch.setattr(a2m_engine, "extract_zip", refusing_extract)

    plan = prepare_run(RunOptions(input_dir=exports, out_dir=results, llm=A2mLlmChoice.FAKE, **opts))
    result = run_batch(plan, setup["stages"])  # type: ignore[arg-type]

    _assert_one_outcome_each(plan, result)
    finished_line = [line for line in log_lines(run_log(results)) if "run finished:" in line][-1]
    numbers = [int(n) for n in re.findall(r"(\d+) (?:done|skipped|refused|failed)", finished_line)]
    assert sum(numbers) == len(plan.selected) + len(plan.selected_rejected), finished_line


def test_CP1_T71_a_zip_with_backslash_folder_entries_unpacks_as_folders(
    tmp_path: Path, results_dir: Path, run_cli
) -> None:
    """[CP1-T71] Windows-style members ('apiproxy\\policies\\'): found, processed, .done, policies/ is a folder."""
    exports = tmp_path / "exports"
    exports.mkdir()
    _write_raw_zip(
        exports / "win.zip",
        [
            ("apiproxy\\win.xml", '<APIProxy name="win"/>\n'),
            ("apiproxy\\policies\\", ""),
            ("apiproxy\\policies\\Quota.xml", "<Quota/>\n"),
        ],
    )
    _write_raw_zip(exports / "win2.zip", [("apiproxy\\win2.xml", "<APIProxy/>\n"), ("apiproxy\\resources\\", "")])
    seen: dict[str, tuple[bool, ...]] = {}

    def inspect(proxy) -> None:
        bundle = Path(proxy.bundle_dir)
        if proxy.name == "win":
            seen["win"] = (
                (bundle / "apiproxy" / "policies").is_dir(),
                (bundle / "apiproxy" / "policies" / "Quota.xml").is_file(),
                (bundle / "apiproxy" / "win.xml").is_file(),
            )
        else:
            seen["win2"] = ((bundle / "apiproxy" / "resources").is_dir(), (bundle / "apiproxy" / "win2.xml").is_file())

    res = run_cli(base_argv(exports, results_dir), stages=[inspect])

    assert res.code == 0, res.err
    assert_no_traceback(res.err, res.out)
    assert "2 done" in res.out and "0 refused" in res.out and "0 failed" in res.out, res.out
    assert seen == {"win": (True, True, True), "win2": (True, True)}
    assert marker_names(results_dir) == {"win", "win2"}


@pytest.mark.parametrize(
    ("name", "is_dir"),
    [
        pytest.param("apiproxy/", True, id="CP1-T72-slash-folder"),
        pytest.param("apiproxy\\policies\\", True, id="CP1-T72-backslash-folder"),
        pytest.param("apiproxy/policies\\", True, id="CP1-T72-mixed-separators-folder"),
        pytest.param("apiproxy/keep/.", True, id="CP1-T72-trailing-dot-part-folder"),
        pytest.param("apiproxy\\x.xml", False, id="CP1-T72-backslash-file"),
        pytest.param("apiproxy/x.xml", False, id="CP1-T72-slash-file"),
    ],
)
def test_CP1_T72_member_is_dir_follows_member_parts(tmp_path: Path, name: str, is_dir: bool) -> None:
    """[CP1-T72] One folder test for members, on every platform; check and unpack agree with it."""
    from a2m.discovery import extract_zip, member_parts

    info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
    assert member_is_dir(info) is is_dir
    zip_path = tmp_path / "b.zip"
    _write_raw_zip(zip_path, [("apiproxy\\b.xml", "<APIProxy/>\n"), (name, "")])
    with zipfile.ZipFile(zip_path) as zf:
        check_zip_members(zf.infolist())
    dest = tmp_path / "out"
    extract_zip(zip_path, dest)
    target = dest.joinpath(*member_parts(name))
    assert target.is_dir() is is_dir
    assert target.is_file() is (not is_dir)


def test_CP1_T73_zip_member_kind_is_decided_only_by_member_is_dir() -> None:
    """[CP1-T73] Structural sweep: no a2m module calls ZipInfo.is_dir() on a zip member."""
    for path in _A2M_SOURCES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "is_dir"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"info", "zinfo", "member"}
            ):
                pytest.fail(f"{path.name}:{node.lineno} calls ZipInfo.is_dir(); use member_is_dir")


# ---------------------------------------------------------------- adversarial round 06 additions
# New imports for the cases below live here so no existing line changes.

from a2m import runlog as a2m_runlog  # noqa: E402

_NO_SHUTDOWN_NOISE = ("Traceback (most recent call last)", "Exception ignored", "Logging error")


def _alpha_exports(tmp_path: Path) -> Path:
    exports = tmp_path / "ex"
    (exports / "alpha" / "apiproxy").mkdir(parents=True)
    (exports / "alpha" / "apiproxy" / "alpha.xml").write_text('<APIProxy name="alpha"/>\n', encoding="utf-8")
    return exports


def _assert_quiet(text: str) -> None:
    for noise in _NO_SHUTDOWN_NOISE:
        assert noise not in text, text


@pytest.mark.skipif(os.name != "posix", reason="pipes, /dev/full and closed descriptors are POSIX")
@pytest.mark.parametrize(
    "case",
    [
        pytest.param("broken-pipe", id="CP1-T74-stdout-broken-pipe"),
        pytest.param("dev-full", id="CP1-T74-stdout-dev-full"),
        pytest.param("closed", id="CP1-T74-stdout-closed"),
    ],
)
def test_CP1_T74_console_script_survives_an_unwritable_stdout(
    tmp_path: Path, subprocess_env: dict[str, str], case: str
) -> None:
    """[CP1-T74] Installed a2m, stdout a dead pipe, /dev/full or closed: exit 0, no traceback, .done written."""
    exports = _alpha_exports(tmp_path)
    results = tmp_path / "res"
    argv = [str(_VENV_BIN / "a2m"), *base_argv(exports, results)]
    if case == "broken-pipe":
        read_end, write_end = os.pipe()
        os.close(read_end)  # the reader is gone before a2m writes, like `a2m ... | true`
        try:
            proc = subprocess.run(
                argv, stdout=write_end, stderr=subprocess.PIPE, cwd=tmp_path, env=subprocess_env, timeout=60
            )
        finally:
            os.close(write_end)
    elif case == "dev-full":
        if not os.path.exists("/dev/full"):
            pytest.skip("no /dev/full on this platform")
        with open("/dev/full", "wb") as full:
            proc = subprocess.run(
                argv, stdout=full, stderr=subprocess.PIPE, cwd=tmp_path, env=subprocess_env, timeout=60
            )
    else:
        proc = subprocess.run(
            ["sh", "-c", 'exec "$@" >&-', "sh", *argv],
            stderr=subprocess.PIPE,
            cwd=tmp_path,
            env=subprocess_env,
            timeout=60,
        )

    err = proc.stderr.decode("utf-8", "backslashreplace")
    assert proc.returncode == 0, err
    _assert_quiet(err)
    assert err == ""
    assert marker_names(results) == {"alpha"}
    assert any("found proxy alpha" in line for line in log_lines(run_log(results)))


@pytest.mark.skipif(os.name != "posix", reason="pipes and closed descriptors are POSIX")
@pytest.mark.parametrize(
    ("case", "error"),
    [
        pytest.param("closed", "unrecognized-argument", id="CP1-T74-stderr-closed-argparse-usage-error"),
        pytest.param("closed", "missing-input", id="CP1-T74-stderr-closed-missing-input"),
        pytest.param("broken-pipe", "unrecognized-argument", id="CP1-T74-stderr-broken-pipe-usage-error"),
        pytest.param("broken-pipe", "missing-input", id="CP1-T74-stderr-broken-pipe-missing-input"),
    ],
)
def test_CP1_T74_console_script_usage_error_with_an_unwritable_stderr_exits_2(
    tmp_path: Path, subprocess_env: dict[str, str], case: str, error: str
) -> None:
    """[CP1-T74] Installed a2m, usage error while stderr is closed or a dead pipe: still exit 2, no output."""
    results = tmp_path / "res"
    if error == "missing-input":
        argv = [str(_VENV_BIN / "a2m"), *base_argv(tmp_path / "missing", results)]
    else:
        argv = [str(_VENV_BIN / "a2m"), *base_argv(tmp_path, results), "--no-such-flag"]
    if case == "closed":
        proc = subprocess.run(
            ["sh", "-c", 'exec "$@" 2>&-', "sh", *argv],
            stdout=subprocess.PIPE,
            cwd=tmp_path,
            env=subprocess_env,
            timeout=60,
        )
    else:
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            proc = subprocess.run(
                argv, stdout=subprocess.PIPE, stderr=write_end, cwd=tmp_path, env=subprocess_env, timeout=60
            )
        finally:
            os.close(write_end)

    out = proc.stdout.decode("utf-8", "backslashreplace")
    assert proc.returncode == 2, out
    assert out == ""
    assert not results.exists()


class _UnwritableStream:
    """A terminal stream that fails every write and flush the way a real one can."""

    encoding = "utf-8"

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def write(self, text: str) -> int:
        raise self.exc

    def flush(self) -> None:
        raise self.exc

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno")


_UNWRITABLE = [
    pytest.param(lambda: BrokenPipeError(errno.EPIPE, "Broken pipe"), id="broken-pipe"),
    pytest.param(lambda: OSError(errno.ENOSPC, "No space left on device"), id="disk-full"),
    pytest.param(lambda: ValueError("I/O operation on closed file."), id="closed-file"),
    pytest.param(None, id="none"),
]


@pytest.mark.parametrize("make_exc", _UNWRITABLE)
@pytest.mark.parametrize("crash", [pytest.param(False, id="CP1-T75-ok"), pytest.param(True, id="CP1-T75-crash")])
def test_CP1_T75_an_unwritable_stdout_never_changes_the_batch_exit_code(
    mixed_exports: Path, results_dir: Path, recorder, crashing_stage, monkeypatch: pytest.MonkeyPatch,
    make_exc, crash: bool
) -> None:
    """[CP1-T75] In-process: stdout that raises (EPIPE, ENOSPC, closed) or is None: exit 0 or 1 as the batch says."""
    stream = None if make_exc is None else _UnwritableStream(make_exc())
    stages = [crashing_stage, recorder] if crash else [recorder]
    monkeypatch.setattr(sys, "stdout", stream)
    try:
        code = a2m_main(base_argv(mixed_exports, results_dir), stages=stages)
    finally:
        monkeypatch.undo()
    assert code == (1 if crash else 0)
    assert marker_names(results_dir) == ({"alpha", "gamma"} if crash else ALL3)


@pytest.mark.parametrize("make_exc", _UNWRITABLE)
def test_CP1_T75_an_unwritable_stderr_keeps_usage_and_interrupt_exit_codes(
    tmp_path: Path, mixed_exports: Path, results_dir: Path, monkeypatch: pytest.MonkeyPatch, make_exc
) -> None:
    """[CP1-T75] In-process: stderr that raises or is None: usage errors still exit 2, Ctrl-C still exits 130."""

    def interrupt(proxy) -> None:
        raise KeyboardInterrupt

    codes = []
    for argv, stages in (
        ([*base_argv(tmp_path, results_dir), "--no-such-flag"], None),
        (base_argv(tmp_path / "missing", results_dir), None),
        (base_argv(mixed_exports, results_dir), [interrupt]),
    ):
        stream = None if make_exc is None else _UnwritableStream(make_exc())
        monkeypatch.setattr(sys, "stderr", stream)
        try:
            try:
                code = a2m_main(argv, stages=stages)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 2
        finally:
            monkeypatch.undo()
        codes.append(code)
    assert codes == [2, 2, 130]


class _FlakyFileIO(io.FileIO):
    """run.log's raw file, failing with ENOSPC on the writes ``fail`` picks (1-based)."""

    def __init__(self, fd: int, fail) -> None:
        super().__init__(fd, "a")
        self.fail = fail
        self.writes = 0

    def write(self, data) -> int:  # type: ignore[override]
        self.writes += 1
        if self.fail(self.writes):
            raise OSError(errno.ENOSPC, "No space left on device")
        return super().write(data)


def _flaky_run_log(monkeypatch: pytest.MonkeyPatch, fail) -> None:
    def flaky_open(self):
        raw = _FlakyFileIO(self._opener(), fail)
        return io.TextIOWrapper(io.BufferedWriter(raw), encoding="utf-8", errors="backslashreplace")

    monkeypatch.setattr(a2m_runlog._OpenerFileHandler, "_open", flaky_open)


@pytest.mark.parametrize(
    "fail",
    [
        pytest.param(lambda n: 3 <= n <= 5, id="CP1-T76-a-few-records"),
        pytest.param(lambda n: n >= 3, id="CP1-T76-disk-stays-full"),
    ],
)
def test_CP1_T76_a_run_log_write_failure_is_one_stderr_line_and_exit_1(
    mixed_exports: Path, results_dir: Path, run_cli, recorder, monkeypatch: pytest.MonkeyPatch, fail
) -> None:
    """[CP1-T76] run.log writes fail with ENOSPC: no 'Logging error' traceback, one stderr line naming run.log, exit 1."""
    _flaky_run_log(monkeypatch, fail)

    res = run_cli(base_argv(mixed_exports, results_dir), stages=[recorder])

    assert res.code == 1, (res.out, res.err)
    _assert_quiet(res.err)
    lines = res.err.splitlines()
    assert len(lines) == 1, res.err
    assert "run.log" in lines[0] and "incomplete" in lines[0], lines[0]
    assert "No space left on device" in lines[0], lines[0]
    assert marker_names(results_dir) == ALL3


def test_CP1_T76_ctrl_c_still_wins_over_a_failing_run_log(
    mixed_exports: Path, results_dir: Path, run_cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP1-T76] Ctrl-C while run.log cannot be written: exit 130 with the one 'interrupted' line, no traceback."""
    _flaky_run_log(monkeypatch, lambda n: n >= 3)

    def interrupt(proxy) -> None:
        raise KeyboardInterrupt

    res = run_cli(base_argv(mixed_exports, results_dir), stages=[interrupt])

    assert res.code == 130, (res.out, res.err)
    _assert_quiet(res.err)
    lines = res.err.splitlines()
    assert len(lines) == 1 and "interrupted" in lines[0], res.err


def test_CP1_T76_a_healthy_run_log_raises_nothing(mixed_exports: Path, results_dir: Path, run_cli, recorder) -> None:
    """[CP1-T76] Guard: with a writable run.log the run still exits 0 with an empty stderr."""
    res = run_cli(base_argv(mixed_exports, results_dir), stages=[recorder])
    assert res.code == 0, res.err
    assert res.err == ""


_HANDLER_FACTORIES = {"FileHandler", "StreamHandler", "WatchedFileHandler", "RotatingFileHandler",
                      "TimedRotatingFileHandler", "Handler"}


def test_CP1_T77_log_handlers_never_fall_back_to_stderr_or_open_files_unguarded() -> None:
    """[CP1-T77] Structural: every a2m handler class overrides handleError; no module builds a stdlib file handler."""
    problems: list[str] = []
    handler_classes = 0
    for path in _A2M_SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            where = f"{path.name}:{getattr(node, 'lineno', '?')}"
            if isinstance(node, ast.ClassDef):
                base_names = {b.attr if isinstance(b, ast.Attribute) else getattr(b, "id", "") for b in node.bases}
                if base_names & _HANDLER_FACTORIES:
                    handler_classes += 1
                    methods = {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
                    if "handleError" not in methods:
                        problems.append(f"{where} handler {node.name} does not override handleError")
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name in _HANDLER_FACTORIES:
                    problems.append(f"{where} constructs logging.{name} directly")
    assert problems == [], "\n".join(problems)
    assert handler_classes >= 1


@pytest.mark.parametrize(
    ("kind", "outcome"),
    [
        pytest.param("zip-apiproxy-file", "skipped", id="CP1-T78-zip-apiproxy-is-a-file"),
        pytest.param("folder-apiproxy-file", "skipped", id="CP1-T78-folder-apiproxy-is-a-file"),
        pytest.param("zip-apiproxy-file-and-sharedflow", "shared", id="CP1-T78-zip-file-apiproxy-plus-sharedflow"),
        pytest.param(
            "folder-apiproxy-file-and-sharedflow", "shared", id="CP1-T78-folder-file-apiproxy-plus-sharedflow"
        ),
        pytest.param("zip-sharedflow-file", "skipped", id="CP1-T78-zip-sharedflowbundle-is-a-file"),
        pytest.param("zip-empty-apiproxy-folder", "proxy", id="CP1-T78-zip-empty-apiproxy-folder-entry"),
        pytest.param("zip-dot-slash-apiproxy", "proxy", id="CP1-T78-zip-dot-slash-apiproxy"),
    ],
)
def test_CP1_T78_apiproxy_counts_only_as_a_folder_for_zips_and_folders_alike(
    tmp_path: Path, results_dir: Path, run_cli, recorder, make_bundle, kind: str, outcome: str
) -> None:
    """[CP1-T78] A plain file named apiproxy is not a bundle, in a zip exactly as in a folder; a folder entry is."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "alpha")
    zip_members = {
        "zip-apiproxy-file": [("apiproxy", "not a folder\n")],
        "zip-apiproxy-file-and-sharedflow": [("apiproxy", "x\n"), ("sharedflowbundle/stray.xml", "<SharedFlow/>\n")],
        "zip-sharedflow-file": [("sharedflowbundle", "x\n")],
        "zip-empty-apiproxy-folder": [("apiproxy/", "")],
        "zip-dot-slash-apiproxy": [("./apiproxy/stray.xml", "<APIProxy/>\n")],
    }
    if kind in zip_members:
        _write_raw_zip(exports / "stray.zip", zip_members[kind])
    else:
        folder = exports / "stray"
        folder.mkdir()
        (folder / "apiproxy").write_text("not a folder\n", encoding="utf-8")
        if kind.endswith("sharedflow"):
            (folder / "sharedflowbundle").mkdir()
            (folder / "sharedflowbundle" / "stray.xml").write_text("<SharedFlow/>\n", encoding="utf-8")

    res = run_cli(base_argv(exports, results_dir), stages=[recorder])

    assert res.code == 0, res.err
    lines = log_lines(run_log(results_dir))
    found_proxy = any("found proxy stray" in line for line in lines)
    found_shared = any("found shared flow bundle stray" in line for line in lines)
    skipped = any("skipped stray" in line and "not a bundle" in line for line in lines)
    assert (found_proxy, found_shared, skipped) == (outcome == "proxy", outcome == "shared", outcome == "skipped"), lines
    assert marker_names(results_dir) == ({"alpha", "stray"} if outcome == "proxy" else {"alpha"})
    assert ("2 done" if outcome == "proxy" else "1 done") in res.out


# --- CP1 adversarial round 07: one naming rule for every input item, readable or not ---

from a2m import discovery as a2m_discovery  # noqa: E402


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("folder", id="CP1-T80-unreadable-folder-named-alpha.zip"),
        pytest.param("folder-upper", id="CP1-T80-unreadable-folder-named-alpha.ZIP"),
    ],
)
@needs_non_root
def test_CP1_T80_unreadable_folder_ending_in_zip_keeps_its_folder_name(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, recorder, kind: str
) -> None:
    """[CP1-T80] A mode-000 folder called alpha.zip is refused as 'alpha.zip' and never knocks out a valid alpha/."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "alpha")
    folder_name = "alpha.zip" if kind == "folder" else "alpha.ZIP"
    locked = exports / folder_name
    locked.mkdir()
    (locked / "apiproxy").mkdir()
    os.chmod(locked, 0)
    try:
        res = run_cli(base_argv(exports, results_dir), stages=[recorder])
    finally:
        os.chmod(locked, 0o700)

    assert res.code == 0, res.err
    assert_no_traceback(res.err, res.out)
    assert recorder.calls == ["alpha"]
    assert marker_names(results_dir) == {"alpha"}
    text = run_log(results_dir)
    assert "name conflict" not in text, text
    refused = [line for line in log_lines(text) if "refused" in line and "ERROR" in line]
    assert len(refused) == 1, text
    assert f"refused {folder_name} ({folder_name}): cannot read this item" in refused[0], refused
    assert "1 done" in res.out
    assert "1 refused" in res.out
    assert "0 failed" in res.out


@needs_non_root
def test_CP1_T80_unreadable_zip_file_still_conflicts_by_its_stem(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, recorder
) -> None:
    """[CP1-T80] Regression guard: an unreadable zip FILE alpha.zip is still named alpha, so it conflicts with alpha/."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "alpha")
    locked = exports / "alpha.zip"
    _write_raw_zip(locked, [("apiproxy/alpha.xml", "<APIProxy/>\n")])
    os.chmod(locked, 0)
    try:
        res = run_cli(base_argv(exports, results_dir), stages=[recorder])
    finally:
        os.chmod(locked, 0o600)

    assert res.code == 0, res.err
    assert recorder.calls == []
    assert marker_names(results_dir) == set()
    assert "name conflict: alpha, alpha.zip share the same proxy name" in run_log(results_dir)


def test_CP1_T81_item_name_is_the_one_naming_rule(tmp_path: Path, make_bundle) -> None:
    """[CP1-T81] item_name: zip stem only for a regular .zip file; full name for folders and everything else."""
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / "dir.zip").mkdir()
    (exports / "file.zip").write_bytes(b"")
    (exports / "UPPER.ZIP").write_bytes(b"")
    (exports / "notes.txt").write_bytes(b"")
    (exports / "plain").mkdir()
    (exports / "link.zip").symlink_to(exports / "file.zip")
    (exports / "dirlink.zip").symlink_to(exports / "dir.zip")
    (exports / "dangling.zip").symlink_to(exports / "missing")
    expected = {
        "dir.zip": "dir.zip",
        "file.zip": "file",
        "UPPER.ZIP": "UPPER",
        "notes.txt": "notes.txt",
        "plain": "plain",
        "link.zip": "link",
        "dirlink.zip": "dirlink.zip",
        "dangling.zip": "dangling.zip",
    }
    if hasattr(os, "mkfifo"):
        os.mkfifo(exports / "pipe.zip")
        expected["pipe.zip"] = "pipe.zip"
    got = {path.name: a2m_discovery.item_name(path) for path in exports.iterdir()}
    assert got == expected


def test_CP1_T81_every_discovered_item_is_named_by_item_name(tmp_path: Path, make_bundle) -> None:
    """[CP1-T81] Every proxy, shared flow and refused item from discover() carries item_name(path)."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "alpha")
    make_bundle(exports, "beta.zip")
    _write_raw_zip(exports / "gamma.zip", [("apiproxy/gamma.xml", "<APIProxy/>\n")])
    _write_raw_zip(exports / "flow.zip", [("sharedflowbundle/flow.xml", "<SharedFlow/>\n")])
    (exports / "corrupt.zip").write_bytes(b"not a zip")
    _write_raw_zip(exports / "slip.zip", [("../evil.xml", "x")])
    found = a2m_discovery.discover(exports)
    items = [*found.proxies, *found.shared_flows, *found.rejected]
    assert {item.path.name for item in items} == {
        "alpha", "beta.zip", "gamma.zip", "flow.zip", "corrupt.zip", "slip.zip"
    }
    for item in items:
        assert item.name == a2m_discovery.item_name(item.path), item


def test_CP1_T82_discovery_names_items_only_through_item_name() -> None:
    """[CP1-T82] Structural: item_name is the only place an input item's name is derived, in every path."""
    problems: list[str] = []
    for path in _A2M_SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        for func in funcs:
            for node in ast.walk(func):
                if isinstance(node, ast.Attribute) and node.attr == "stem" and func.name != "item_name":
                    problems.append(f"{path.name}:{node.lineno} {func.name} uses .stem; name items with item_name")
    assert problems == [], "\n".join(problems)


# --- CP1 adversarial round 08: one wrapper folder is accepted, other misplaced bundles are refused; bad --out ---

from a2m.discovery import (  # noqa: E402
    BundleRoot,
    bundle_root,
    folder_subfolders,
    zip_folder_tree,
    zip_subfolders,
    zip_top_folders,
)
from a2m.errors import BundleLayoutError  # noqa: E402

_R8_PROXY = {
    "apiproxy/alpha.xml": '<APIProxy name="alpha"/>\n',
    "apiproxy/proxies/default.xml": '<ProxyEndpoint name="default"/>\n',
}


def _r8_members(prefix: str) -> list[tuple[str, str]]:
    return [(f"{prefix}{rel}", text) for rel, text in _R8_PROXY.items()]


def _r8_write_folder(root: Path, members: list[tuple[str, str]]) -> None:
    for rel, text in members:
        path = root / rel
        if rel.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _r8_bundle_seer(seen: dict[str, bool]):
    def stage(proxy) -> None:
        seen[proxy.name] = (Path(proxy.bundle_dir) / "apiproxy" / f"{proxy.name}.xml").is_file()

    return stage


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("zip-wrapper", id="CP1-T83-zip-wrapped-in-one-folder"),
        pytest.param("zip-wrapper-macosx", id="CP1-T83-finder-compress-zip-with-__MACOSX"),
        pytest.param("zip-wrapper-other-name", id="CP1-T83-zip-wrapper-folder-named-differently"),
        pytest.param("folder-wrapper", id="CP1-T83-folder-wrapped-in-one-folder"),
    ],
)
def test_CP1_T83_a_bundle_wrapped_in_one_top_folder_is_processed_from_that_folder(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, kind: str
) -> None:
    """[CP1-T83] alpha.zip holding alpha/apiproxy/ (Finder 'Compress', zip -r) is a proxy; run.log notes the wrapper."""
    exports = tmp_path / "in3"
    exports.mkdir()
    make_bundle(exports, "gamma")
    wrapper = "export-v1" if kind == "zip-wrapper-other-name" else "alpha"
    members = _r8_members(f"{wrapper}/")
    if kind == "zip-wrapper-macosx":
        members += [("__MACOSX/alpha/apiproxy/._alpha.xml", "resource fork\n"), ("__MACOSX/alpha/._apiproxy", "x\n")]
    if kind.startswith("zip"):
        _write_raw_zip(exports / "alpha.zip", members)
        item = "alpha.zip"
    else:
        _r8_write_folder(exports / "alpha", members)
        item = "alpha"
    seen: dict[str, bool] = {}

    res = run_cli(base_argv(exports, results_dir), stages=[_r8_bundle_seer(seen)])

    assert res.code == 0, res.err
    assert res.out.startswith("a2m: 2 done, 0 skipped as already done, 0 refused, 0 failed."), res.out
    assert seen == {"alpha": True, "gamma": True}
    assert marker_names(results_dir) == {"alpha", "gamma"}
    found = [line for line in log_lines(run_log(results_dir)) if "found proxy alpha" in line]
    assert len(found) == 1, found
    assert f"({'zip' if kind.startswith('zip') else 'folder'} {item}, " in found[0], found
    assert f"top folder {wrapper}/" in found[0], found
    assert "ERROR" not in run_log(results_dir)


def test_CP1_T83_a_wrapped_shared_flow_is_logged_as_a_shared_flow(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, recorder
) -> None:
    """[CP1-T83] flow.zip holding flow/sharedflowbundle/ is found as a shared flow, with its wrapper noted."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "gamma")
    _write_raw_zip(exports / "flow.zip", [("flow/sharedflowbundle/flow.xml", "<SharedFlowBundle/>\n")])

    res = run_cli(base_argv(exports, results_dir), stages=[recorder])

    assert res.code == 0, res.err
    assert recorder.calls == ["gamma"]
    lines = [line for line in log_lines(run_log(results_dir)) if "found shared flow bundle flow" in line]
    assert len(lines) == 1 and "top folder flow/" in lines[0], lines


_R8_REFUSED = {
    "zip-nested-deeper": (
        "zip",
        _r8_members("exports/alpha/"),
        ["exports/alpha/apiproxy/", "2 folders down"],
    ),
    "zip-two-wrapped-bundles": (
        "zip",
        [("a/apiproxy/a.xml", "<APIProxy/>\n"), ("b/apiproxy/b.xml", "<APIProxy/>\n")],
        ["2 bundles one folder down", "a/apiproxy/", "b/apiproxy/"],
    ),
    "zip-proxy-and-flow-wrapped": (
        "zip",
        [("a/apiproxy/a.xml", "<APIProxy/>\n"), ("b/sharedflowbundle/b.xml", "<SharedFlowBundle/>\n")],
        ["2 bundles one folder down", "a/apiproxy/", "b/sharedflowbundle/"],
    ),
    "folder-nested-deeper": (
        "folder",
        _r8_members("x/y/"),
        ["x/y/apiproxy/", "2 folders down"],
    ),
    "folder-two-wrapped-bundles": (
        "folder",
        [("a/apiproxy/a.xml", "<APIProxy/>\n"), ("b/apiproxy/b.xml", "<APIProxy/>\n")],
        ["2 bundles one folder down", "a/apiproxy/", "b/apiproxy/"],
    ),
}


def _r8_write_item(exports: Path, kind: str) -> str:
    shape, members, _ = _R8_REFUSED[kind]
    if shape == "zip":
        _write_raw_zip(exports / "alpha.zip", members)
        return "alpha.zip"
    _r8_write_folder(exports / "alpha", members)
    return "alpha"


@pytest.mark.parametrize("alone", [pytest.param(False, id="next-to-a-proxy"), pytest.param(True, id="alone")])
@pytest.mark.parametrize("kind", [pytest.param(k, id=f"CP1-T84-{k}") for k in _R8_REFUSED])
def test_CP1_T84_a_misplaced_bundle_is_refused_and_counted_never_skipped(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, recorder, kind: str, alone: bool
) -> None:
    """[CP1-T84] A bundle root nested deeper, or several one folder down: refused at ERROR naming the path, counted."""
    exports = tmp_path / "exports"
    exports.mkdir()
    if not alone:
        make_bundle(exports, "gamma")
    item = _r8_write_item(exports, kind)

    res = run_cli(base_argv(exports, results_dir), stages=[recorder])

    assert res.code == 0, res.err
    assert_no_traceback(res.err, res.out)
    done = 0 if alone else 1
    assert res.out.startswith(f"a2m: {done} done, 0 skipped as already done, 1 refused, 0 failed."), res.out
    assert recorder.calls == ([] if alone else ["gamma"])
    lines = log_lines(run_log(results_dir))
    refused = [line for line in lines if f"refused alpha ({item}):" in line]
    assert len(refused) == 1, lines
    assert " ERROR " in refused[0], refused
    for part in _R8_REFUSED[kind][2]:
        assert part in refused[0], refused
    assert not any("skipped" in line and item in line for line in lines), lines
    assert "no proxies found" not in res.err


@pytest.mark.parametrize("kind", [pytest.param(k, id=f"CP1-T84-conflict-{k}") for k in ("zip-nested-deeper",)])
def test_CP1_T84_a_refused_misplaced_bundle_still_takes_part_in_the_name_check(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, recorder, kind: str
) -> None:
    """[CP1-T84] A refused alpha.zip with a nested bundle next to a valid alpha/ is a name conflict: neither runs."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "alpha")
    _r8_write_item(exports, kind)

    res = run_cli(base_argv(exports, results_dir), stages=[recorder])

    assert res.code == 0, res.err
    assert recorder.calls == []
    assert "name conflict: alpha, alpha.zip share the same proxy name" in run_log(results_dir)
    assert "2 refused" in res.out


_R8_LAYOUTS = {
    "top": (_r8_members(""), BundleRoot("apiproxy", None)),
    "top-shared-flow": ([("sharedflowbundle/f.xml", "x")], BundleRoot("sharedflowbundle", None)),
    "top-wins-over-wrapper": (_r8_members("") + _r8_members("w/"), BundleRoot("apiproxy", None)),
    "wrapper": (_r8_members("w/"), BundleRoot("apiproxy", "w")),
    "wrapper-proxy-wins-over-flow": (
        _r8_members("w/") + [("w/sharedflowbundle/f.xml", "x")],
        BundleRoot("apiproxy", "w"),
    ),
    "wrapper-empty-folder-entry": ([("w/apiproxy/", "")], BundleRoot("apiproxy", "w")),
    "wrapper-with-stray-files": (_r8_members("w/") + [("README.md", "x"), ("docs/a.md", "x")], BundleRoot("apiproxy", "w")),
    "wrapper-apiproxy-is-a-file": ([("w/apiproxy", "x")], None),
    "nothing-bundle-like": ([("docs/a.md", "x"), ("notes.txt", "x")], None),
    "nested": (_r8_members("a/b/c/"), "found a/b/c/apiproxy/, 3 folders down"),
    "two-wrapped": (_r8_members("a/") + _r8_members("b/"), "2 bundles one folder down (a/apiproxy/, b/apiproxy/)"),
}


@pytest.mark.parametrize("layout", [pytest.param(k, id=f"CP1-T85-{k}") for k in _R8_LAYOUTS])
def test_CP1_T85_bundle_root_decides_the_same_for_a_folder_and_a_zip(tmp_path: Path, layout: str) -> None:
    """[CP1-T85] Structural: one layout rule, bundle_root, gives the same answer for a folder and a zip of it."""
    members, expected = _R8_LAYOUTS[layout]
    folder = tmp_path / "item"
    folder.mkdir()
    _r8_write_folder(folder, members)
    zip_path = tmp_path / "item.zip"
    _write_raw_zip(zip_path, members)
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()

    answers = []
    for subfolders in (folder_subfolders(folder), zip_subfolders(infos)):
        try:
            answers.append(bundle_root(subfolders))
        except BundleLayoutError as exc:
            answers.append(str(exc))
    assert answers[0] == answers[1], answers
    if isinstance(expected, str):
        assert isinstance(answers[0], str) and expected in answers[0], answers
    else:
        assert answers[0] == expected, answers
    assert zip_folder_tree(infos)[()] == zip_top_folders(infos)


def test_CP1_T85_a_link_loop_inside_a_docs_folder_does_not_hang_discovery(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, recorder
) -> None:
    """[CP1-T85] The search for a misplaced bundle does not follow links below the wrapper level, so loops end."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "gamma")
    docs = exports / "docs"
    (docs / "a").mkdir(parents=True)
    (docs / "loop").symlink_to(docs)
    (docs / "a" / "loop").symlink_to(docs)
    (docs / "a" / "up").symlink_to(exports)

    res = run_cli(base_argv(exports, results_dir), stages=[recorder])

    assert res.code == 0, res.err
    assert recorder.calls == ["gamma"]
    assert any("skipped docs" in line and "not a bundle" in line for line in log_lines(run_log(results_dir)))


@pytest.mark.parametrize(
    "state",
    [
        pytest.param("dangling-link", id="CP1-T86-out-is-a-dangling-link"),
        pytest.param("under-a-file", id="CP1-T86-out-under-a-plain-file"),
        pytest.param("under-a-dangling-link", id="CP1-T86-out-under-a-dangling-link"),
        pytest.param("under-a-link-to-a-file", id="CP1-T86-out-under-a-link-to-a-file"),
    ],
)
def test_CP1_T86_an_unusable_out_path_is_one_usage_line_exit_2_and_nothing_changes(
    mixed_exports: Path, tmp_path: Path, run_cli, recorder, state: str
) -> None:
    """[CP1-T86] --out that cannot become a results folder: exit 2, one stderr line naming it, nothing written."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "file.txt").write_text("keep\n", encoding="utf-8")
    if state == "dangling-link":
        (work / "dang").symlink_to(tmp_path / "nowhere")
        out = work / "dang"
        blamed = str(tmp_path / "nowhere")
    elif state == "under-a-file":
        out = work / "file.txt" / "results"
        blamed = str(work / "file.txt")
    elif state == "under-a-dangling-link":
        (work / "dang").symlink_to(tmp_path / "nowhere")
        out = work / "dang" / "results"
        blamed = str(work / "dang")
    else:
        (work / "flink").symlink_to(work / "file.txt")
        out = work / "flink" / "results"
        blamed = str(work / "flink")
    before = _tree_state(work)

    res = run_cli(base_argv(mixed_exports, out), stages=[recorder])

    assert res.code == 2, (res.out, res.err)
    line = _one_stderr_line(res.err)
    assert "usage error" in line and str(out) in line and blamed in line, line
    assert "File exists" not in line, line
    assert_no_traceback(res.err, res.out)
    assert recorder.calls == []
    assert _tree_state(work) == before
    assert not (tmp_path / "nowhere").exists()


@needs_non_root
def test_CP1_T86_a_results_folder_that_cannot_be_created_is_a_usage_error(
    mixed_exports: Path, tmp_path: Path, run_cli, recorder
) -> None:
    """[CP1-T86] mkdir of --out failing (read-only parent) is one usage line and exit 2, not exit 1."""
    parent = tmp_path / "ro"
    parent.mkdir()
    os.chmod(parent, 0o500)
    try:
        res = run_cli(base_argv(mixed_exports, parent / "results"), stages=[recorder])
    finally:
        os.chmod(parent, 0o700)

    assert res.code == 2, (res.out, res.err)
    line = _one_stderr_line(res.err)
    assert "usage error" in line and "cannot create results folder" in line and str(parent / "results") in line, line
    assert recorder.calls == []


def test_CP1_T86_an_input_folder_that_is_a_dangling_link_says_so(
    tmp_path: Path, results_dir: Path, run_cli, recorder
) -> None:
    """[CP1-T86] EXPORTS as a dangling link: exit 2, one line naming the link and its missing target."""
    exports = tmp_path / "exports"
    exports.symlink_to(tmp_path / "unmounted")

    res = run_cli(base_argv(exports, results_dir), stages=[recorder])

    assert res.code == 2, res.err
    line = _one_stderr_line(res.err)
    assert str(exports) in line and str(tmp_path / "unmounted") in line and "symbolic link" in line, line
    assert not results_dir.exists()


# --- CP1 adversarial round 09: one total classification per input item; overlap checks by file identity ---

from a2m import pathid as a2m_pathid  # noqa: E402
from a2m.discovery import BundleSource, RejectedItem, SkippedItem, classify  # noqa: E402


def _r9_refused_lines(results: Path) -> list[str]:
    return [line for line in log_lines(run_log(results)) if "ERROR" in line and "refused" in line]


def test_CP1_T87_links_in_the_exports_folder_are_refused_naming_their_target(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, recorder
) -> None:
    """[CP1-T87] A dangling zip link, a dangling folder link and a link loop are each refused, never skipped."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "gamma")
    unmounted = tmp_path / "unmounted"
    (exports / "orders.zip").symlink_to(unmounted / "orders.zip")
    (exports / "billing").symlink_to(unmounted / "billing")
    (exports / "loop").symlink_to(exports / "loop")

    res = run_cli(base_argv(exports, results_dir), stages=[recorder])

    assert res.code == 0, res.err
    assert_no_traceback(res.err, res.out)
    assert res.out.startswith("a2m: 1 done, 0 skipped as already done, 3 refused, 0 failed."), res.out
    assert recorder.calls == ["gamma"]
    refused = _r9_refused_lines(results_dir)
    assert len(refused) == 3, refused
    for item, target, words in (
        ("orders.zip", unmounted / "orders.zip", "which does not exist"),
        ("billing", unmounted / "billing", "which does not exist"),
        ("loop", exports / "loop", "symbolic link loop"),
    ):
        mine = [line for line in refused if f"({item}):" in line]
        assert len(mine) == 1, (item, refused)
        assert str(target) in mine[0] and words in mine[0], mine[0]
    lines = log_lines(run_log(results_dir))
    assert not any("not a folder or a .zip file" in line for line in lines), lines
    assert not any(
        line for line in lines if "skipped" in line and any(n in line for n in ("orders", "billing", "loop"))
    ), lines


@pytest.mark.parametrize(
    "state",
    [
        pytest.param("live-folder-link", id="CP1-T87-live-link-to-a-bundle-folder"),
        pytest.param("live-zip-link", id="CP1-T87-live-link-to-a-bundle-zip"),
        pytest.param("dangling-folder-link", id="CP1-T87-dangling-link-where-a-bundle-was"),
    ],
)
def test_CP1_T87_a_link_replacing_a_done_bundle_is_refused_and_its_stale_done_removed(
    tmp_path: Path, results_dir: Path, run_cli, make_bundle, make_zip, recorder, state: str
) -> None:
    """[CP1-T87] Any link is refused (never followed); the earlier output of that name is removed, so --resume redoes it."""
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "gamma")
    make_bundle(exports, "billing")
    assert run_cli(base_argv(exports, results_dir), stages=[recorder]).code == 0
    assert marker_names(results_dir) == {"billing", "gamma"}

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.move(str(exports / "billing"), str(elsewhere / "billing"))
    if state == "live-folder-link":
        (exports / "billing").symlink_to(elsewhere / "billing")
        item, target = "billing", elsewhere / "billing"
    elif state == "live-zip-link":
        make_zip(elsewhere, "billing")
        (exports / "billing.zip").symlink_to(elsewhere / "billing.zip")
        item, target = "billing.zip", elsewhere / "billing.zip"
    else:
        (exports / "billing").symlink_to(tmp_path / "unmounted" / "billing")
        item, target = "billing", tmp_path / "unmounted" / "billing"

    res = run_cli(base_argv(exports, results_dir, "--resume"), stages=[recorder])

    assert res.code == 0, res.err
    assert res.out.startswith("a2m: 0 done, 1 skipped as already done, 1 refused, 0 failed."), res.out
    mine = [line for line in _r9_refused_lines(results_dir) if f"refused billing ({item}):" in line]
    assert len(mine) == 1 and str(target) in mine[0] and "symbolic link" in mine[0], mine
    assert marker_names(results_dir) == {"gamma"}
    assert not (results_dir / "billing").exists()


def _r9_build_states(exports: Path, make_bundle) -> dict[str, type]:
    """Every file type and state an exports item can be in, with the one outcome classify() must give."""
    expected: dict[str, type] = {}
    (exports / "notes.txt").write_text("x\n", encoding="utf-8")
    expected["notes.txt"] = SkippedItem
    _write_raw_zip(exports / "proxy.zip", [("apiproxy/proxy.xml", "<APIProxy/>\n")])
    expected["proxy.zip"] = BundleSource
    # A readable zip with no apiproxy/ folder anywhere is skipped (pinned by CP1-T78).
    _write_raw_zip(exports / "docs.zip", [("docs/a.md", "x\n")])
    expected["docs.zip"] = SkippedItem
    _write_raw_zip(exports / "nested.zip", [("a/b/apiproxy/x.xml", "x\n")])
    expected["nested.zip"] = RejectedItem
    (exports / "corrupt.zip").write_bytes(b"not a zip")
    expected["corrupt.zip"] = RejectedItem
    make_bundle(exports, "proxy-dir")
    expected["proxy-dir"] = BundleSource
    (exports / "docs-dir" / "a").mkdir(parents=True)
    (exports / "docs-dir" / "loop").symlink_to(exports / "docs-dir")
    expected["docs-dir"] = SkippedItem
    (exports / "dir-with-linked-apiproxy").mkdir()
    (exports / "dir-with-linked-apiproxy" / "apiproxy").symlink_to(exports / "proxy-dir" / "apiproxy")
    expected["dir-with-linked-apiproxy"] = RejectedItem
    deep = exports / "too-deep"
    deep.joinpath(*(["d"] * 70)).mkdir(parents=True)
    expected["too-deep"] = RejectedItem
    (exports / "link-to-file").symlink_to(exports / "notes.txt")
    expected["link-to-file"] = RejectedItem
    (exports / "link-to-zip.zip").symlink_to(exports / "proxy.zip")
    expected["link-to-zip.zip"] = RejectedItem
    (exports / "link-to-dir").symlink_to(exports / "proxy-dir")
    expected["link-to-dir"] = RejectedItem
    (exports / "dangling").symlink_to(exports / "missing")
    expected["dangling"] = RejectedItem
    (exports / "dangling.zip").symlink_to(exports / "missing.zip")
    expected["dangling.zip"] = RejectedItem
    (exports / "loop").symlink_to(exports / "loop")
    expected["loop"] = RejectedItem
    (exports / "loop-a").symlink_to(exports / "loop-b")
    (exports / "loop-b").symlink_to(exports / "loop-a")
    expected["loop-a"] = RejectedItem
    expected["loop-b"] = RejectedItem
    if hasattr(os, "mkfifo"):
        os.mkfifo(exports / "pipe")
        os.mkfifo(exports / "pipe.zip")
        expected["pipe"] = RejectedItem
        expected["pipe.zip"] = RejectedItem
    return expected


_R9_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def test_CP1_T88_every_item_state_gets_exactly_one_outcome(tmp_path: Path, make_bundle) -> None:
    """[CP1-T88] Table: each file type/state is exactly one of bundle, refused, skipped; counts sum to the items."""
    exports = tmp_path / "exports"
    exports.mkdir()
    expected = _r9_build_states(exports, make_bundle)
    locked: list[tuple[Path, int]] = []
    if not _R9_IS_ROOT:
        make_bundle(exports, "unreadable-dir")
        _write_raw_zip(exports / "unreadable.zip", [("apiproxy/u.xml", "<APIProxy/>\n")])
        (exports / "unreadable-inner").joinpath("sub").mkdir(parents=True)
        expected.update(
            {"unreadable-dir": RejectedItem, "unreadable.zip": RejectedItem, "unreadable-inner": RejectedItem}
        )
        locked = [
            (exports / "unreadable-dir", 0o700),
            (exports / "unreadable.zip", 0o600),
            (exports / "unreadable-inner" / "sub", 0o700),
        ]
    try:
        for path, _mode in locked:
            os.chmod(path, 0)
        entries = sorted(exports.iterdir(), key=lambda p: p.name)
        assert {p.name for p in entries} == set(expected), sorted(expected)
        outcomes = {p.name: _in_thread(lambda p=p: classify(p, a2m_discovery.item_name(p))) for p in entries}
        found = _in_thread(lambda: a2m_discovery.discover(exports))
    finally:
        for path, mode in locked:
            os.chmod(path, mode)

    for item, outcome in outcomes.items():
        assert type(outcome) is expected[item], (item, outcome)
        assert outcome.name == a2m_discovery.item_name(exports / item), (item, outcome)
        if isinstance(outcome, SkippedItem):
            mode = os.lstat(exports / item).st_mode
            assert stat.S_ISREG(mode) or stat.S_ISDIR(mode), (item, "skipped but neither a plain file nor a folder")
        if isinstance(outcome, RejectedItem) and stat.S_ISLNK(os.lstat(exports / item).st_mode):
            assert "symbolic link" in outcome.reason and os.readlink(exports / item) in outcome.reason, outcome
    lists = [found.proxies, found.shared_flows, found.rejected, found.skipped]
    assert sum(len(items) for items in lists) == len(entries)
    paths = sorted(item.path.name for items in lists for item in items)
    assert paths == sorted(p.name for p in entries)
    by_path = {item.path.name: type(item) for items in lists for item in items}
    assert by_path == expected


def _r9_casefold_identity(path: Path) -> tuple[int, int] | None:
    """file_identity as on a case-insensitive file system: a part missing by spelling matches by letter case."""
    try:
        parts = Path(os.path.abspath(path)).parts
        current = Path(parts[0])
        for part in parts[1:]:
            candidate = current / part
            if not os.path.lexists(candidate):
                matches = sorted(n for n in os.listdir(current) if n.lower() == part.lower())
                if not matches:
                    return None
                candidate = current / matches[0]
            current = candidate
        info = os.stat(current)
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


@pytest.mark.parametrize(
    "relation",
    [
        pytest.param("out-inside-input", id="CP1-T89-out-inside-input-spelled-in-other-case"),
        pytest.param("out-is-input", id="CP1-T89-out-is-input-spelled-in-other-case"),
        pytest.param("input-inside-out", id="CP1-T89-input-inside-out-spelled-in-other-case"),
    ],
)
def test_CP1_T89_overlap_is_caught_on_a_case_insensitive_file_system(
    tmp_path: Path, run_cli, make_bundle, recorder, monkeypatch, relation: str
) -> None:
    """[CP1-T89] Simulated case-insensitive file system: an --out alias by letter case is still refused, exit 2."""
    monkeypatch.setattr(a2m_pathid, "file_identity", _r9_casefold_identity)
    work = tmp_path / "work"
    exports = work / "exports"
    exports.mkdir(parents=True)
    make_bundle(exports, "alpha")
    if relation == "out-inside-input":
        out = work / "EXPORTS" / "results"
    elif relation == "out-is-input":
        out = work / "EXPORTS"
    else:
        out = tmp_path / "WORK"
    before = _tree_state(tmp_path)

    res = run_cli(base_argv(exports, out), stages=[recorder])

    assert res.code == 2, (res.out, res.err)
    line = _one_stderr_line(res.err)
    assert "must not be the input folder" in line and str(out) in line, line
    assert recorder.calls == []
    assert _tree_state(tmp_path) == before


def test_CP1_T89_overlap_is_caught_on_a_real_case_insensitive_file_system(
    tmp_path: Path, run_cli, make_bundle, recorder
) -> None:
    """[CP1-T89] On a real case-insensitive file system (macOS default); skipped on case-sensitive ones (Linux)."""
    (tmp_path / "x").write_text("", encoding="utf-8")
    if not (tmp_path / "X").exists():
        pytest.skip("tmp_path is on a case-sensitive file system")
    exports = tmp_path / "exports"
    exports.mkdir()
    make_bundle(exports, "alpha")

    res = run_cli(base_argv(exports, tmp_path / "EXPORTS" / "results"), stages=[recorder])

    assert res.code == 2, (res.out, res.err)
    assert "must not be the input folder" in _one_stderr_line(res.err)
    assert not (exports / "results").exists()


def test_CP1_T90_is_same_or_inside_compares_by_file_identity(tmp_path: Path, monkeypatch) -> None:
    """[CP1-T90] The identity helper, with a symlinked ancestor as the alias, so the identity walk runs everywhere."""
    real = tmp_path / "real"
    (real / "exports").mkdir(parents=True)
    (tmp_path / "realx").mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    same = a2m_pathid.is_same_or_inside

    def check() -> None:
        assert same(alias / "exports" / "results", real / "exports")
        assert same(alias / "exports" / "a" / "b", real / "exports")
        assert same(alias / "exports", real / "exports")
        assert same(real / "exports", alias)
        assert not same(real / "other", alias / "exports")
        assert not same(tmp_path / "realx", real)
        assert not same(real, real / "exports")
        assert not same(tmp_path / "missing", real / "exports")

    check()
    # With resolve() unable to see through the link, only the file identity walk can find the overlap.
    monkeypatch.setattr(Path, "resolve", lambda self, strict=False: Path(os.path.abspath(self)))
    assert str(Path(alias / "exports").resolve()) == str(alias / "exports")
    check()


def test_CP1_T91_every_input_output_overlap_check_uses_the_identity_helper() -> None:
    """[CP1-T91] Structural: the out-dir check calls pathid.is_same_or_inside both ways; no path-text overlap checks."""
    problems = []
    for path in _A2M_SOURCES:
        if path.name in ("pathid.py", "discovery.py", "safefs.py"):
            continue  # pathid is the helper; discovery/safefs check paths inside their own folder, not input vs output
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr in ("is_relative_to", "samefile", "st_ino", "commonpath"):
                problems.append(f"{path.name}:{node.lineno} uses {node.attr}; use pathid.is_same_or_inside")
    assert problems == [], problems


# ---------------------------------------------------------------- CP1 adversarial round 10 additions
# One design: every bundle (folder or zip) is materialized into a sanitized working copy under the
# results work area before any stage sees it. New imports live here so no existing line changes.

import dataclasses as _r10_dataclasses  # noqa: E402
import socket as _r10_socket  # noqa: E402

from conftest import write_bundle_dir, write_bundle_zip  # noqa: E402

from a2m import engine as _r10_engine  # noqa: E402
from a2m import safefs as _r10_safefs  # noqa: E402


def _r10_secret(tmp_path: Path) -> Path:
    secret = tmp_path / "secret"
    secret.mkdir(exist_ok=True)
    (secret / "key").write_text("TOPSECRET\n", encoding="utf-8")
    return secret


class _R10Reader:
    """A stage that records the proxies it saw and every byte it could read through bundle_dir."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.read: dict[str, str] = {}
        self.contexts: list[object] = []

    def __call__(self, proxy) -> None:
        self.calls.append(proxy.name)
        self.contexts.append(proxy)
        bundle = Path(proxy.bundle_dir)
        text = []
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                text.append(path.read_text(encoding="utf-8", errors="replace"))
        self.read[proxy.name] = "".join(text)


def _r10_plant(case: str, bundle: Path, secret: Path) -> str:
    """Plant one unsafe entry in the folder bundle ``bundle``; return the relative path run.log must name."""
    api = bundle / "apiproxy"
    if case == "file-link-outside":
        (api / "policies").mkdir()
        (api / "policies" / "Key.xml").symlink_to(secret / "key")
        return "apiproxy/policies/Key.xml"
    if case == "folder-link-outside":
        for child in (api / "proxies").iterdir():
            child.unlink()
        (api / "proxies").rmdir()
        (api / "proxies").symlink_to(secret)
        return "apiproxy/proxies"
    if case == "dangling-link":
        (api / "resources").symlink_to(secret / "missing")
        return "apiproxy/resources"
    if case == "fifo":
        (api / f"{bundle.name}.xml").unlink()
        os.mkfifo(api / f"{bundle.name}.xml")
        return f"apiproxy/{bundle.name}.xml"
    if case == "socket":
        sock = _r10_socket.socket(_r10_socket.AF_UNIX)
        try:
            sock.bind(str(api / "s.sock"))
        finally:
            sock.close()
        return "apiproxy/s.sock"
    raise AssertionError(case)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX links, FIFOs and sockets")
@pytest.mark.parametrize("wrapped", [pytest.param(False, id="top"), pytest.param(True, id="wrapped")])
@pytest.mark.parametrize(
    "case",
    [
        pytest.param("file-link-outside", id="CP1-T92-file-link-outside"),
        pytest.param("folder-link-outside", id="CP1-T92-folder-link-outside"),
        pytest.param("dangling-link", id="CP1-T92-dangling-link"),
        pytest.param("fifo", id="CP1-T92-fifo-as-proxy-xml"),
        pytest.param("socket", id="CP1-T92-socket"),
    ],
)
def test_CP1_T92_a_folder_bundle_with_a_link_or_special_file_inside_is_refused(
    tmp_path: Path, results_dir: Path, run_cli, case: str, wrapped: bool
) -> None:
    """[CP1-T92] A1: a link, FIFO or socket anywhere in a folder bundle refuses it; no stage reads it; gamma is done."""
    secret = _r10_secret(tmp_path)
    exports = tmp_path / "exports"
    exports.mkdir()
    if wrapped:
        (exports / "alpha").mkdir()
        bundle = write_bundle_dir(exports / "alpha", "alpha")
        shown_prefix = "alpha/"
    else:
        bundle = write_bundle_dir(exports, "alpha")
        shown_prefix = ""
    write_bundle_dir(exports, "gamma")
    shown = shown_prefix + _r10_plant(case, bundle, secret)
    reader = _R10Reader()

    res = _in_thread(lambda: run_cli(base_argv(exports, results_dir), stages=[reader]))

    assert res.code == 0, res.err
    assert_no_traceback(res.err, res.out)
    assert reader.calls == ["gamma"]
    assert all("TOPSECRET" not in text for text in reader.read.values())
    lines = log_lines(run_log(results_dir))
    refused = [line for line in lines if "refused alpha" in line]
    assert len(refused) == 1 and "ERROR" in refused[0] and shown in refused[0], "\n".join(lines)
    assert not any("processing alpha" in line and "ERROR" in line for line in lines)
    assert "1 done" in res.out and "1 refused" in res.out and "0 failed" in res.out, res.out
    assert marker_names(results_dir) == {"gamma"}
    assert not (results_dir / ".a2m-work").exists()
    assert (secret / "key").read_text(encoding="utf-8") == "TOPSECRET\n"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symbolic links")
def test_CP1_T92_force_on_a_bundle_that_gained_a_link_clears_its_stale_done(
    tmp_path: Path, results_dir: Path, run_cli
) -> None:
    """[CP1-T92] A1: a finished folder bundle that now holds a link is refused on --force and loses its .done."""
    secret = _r10_secret(tmp_path)
    exports = tmp_path / "exports"
    exports.mkdir()
    alpha = write_bundle_dir(exports, "alpha")
    write_bundle_dir(exports, "gamma")
    assert run_cli(base_argv(exports, results_dir), stages=[_R10Reader()]).code == 0
    assert marker_names(results_dir) == {"alpha", "gamma"}
    (alpha / "apiproxy" / "leak.xml").symlink_to(secret / "key")
    reader = _R10Reader()

    res = run_cli(base_argv(exports, results_dir, "--force"), stages=[reader])

    assert res.code == 0, res.err
    assert reader.calls == ["gamma"]
    assert marker_names(results_dir) == {"gamma"}
    assert not (results_dir / "alpha").exists()
    assert "1 refused" in res.out


def _r10_path_values(value: object, seen: set[int] | None = None) -> list[Path]:
    """Every Path reachable from ``value`` through dataclass fields, tuples, lists and dicts."""
    seen = set() if seen is None else seen
    if id(value) in seen:
        return []
    seen.add(id(value))
    if isinstance(value, Path):
        return [value]
    if isinstance(value, str):
        return [Path(value)] if os.sep in value else []
    if _r10_dataclasses.is_dataclass(value) and not isinstance(value, type):
        found: list[Path] = []
        for f in _r10_dataclasses.fields(value):
            found += _r10_path_values(getattr(value, f.name), seen)
        return found
    if isinstance(value, (list, tuple, set, frozenset)):
        return [p for item in value for p in _r10_path_values(item, seen)]
    if isinstance(value, dict):
        return [p for item in value.values() for p in _r10_path_values(item, seen)]
    return []


def test_CP1_T93_no_stage_ever_receives_a_path_inside_the_input_folder(tmp_path: Path, results_dir: Path, run_cli) -> None:
    """[CP1-T93] A1 structural, at run time: for folder, zip and wrapped bundles, no Path in ProxyContext is in EXPORTS."""
    exports = tmp_path / "exports"
    exports.mkdir()
    write_bundle_dir(exports, "alpha")
    write_bundle_zip(exports, "beta")
    (exports / "delta").mkdir()
    write_bundle_dir(exports / "delta", "delta")
    _write_raw_zip(exports / "eps.zip", [("eps/apiproxy/eps.xml", "<APIProxy/>\n")])
    reader = _R10Reader()

    res = run_cli(base_argv(exports, results_dir), stages=[reader])

    assert res.code == 0, res.err
    assert sorted(reader.calls) == ["alpha", "beta", "delta", "eps"]
    work_root = (results_dir / ".a2m-work").resolve()
    for ctx in reader.contexts:
        name = ctx.name  # type: ignore[attr-defined]
        bundle = Path(ctx.bundle_dir).resolve()  # type: ignore[attr-defined]
        assert bundle.is_relative_to(work_root / name), (name, bundle)
        for path in _r10_path_values(ctx):
            assert not pathid_is_inside(path, exports), (name, path)
        assert reader.read[name], name  # the copy really holds the bundle
    for f in _r10_dataclasses.fields(_r10_engine.ProxyContext):
        assert f.name not in ("source", "input_dir"), f.name
    assert {f.name for f in _r10_dataclasses.fields(_r10_engine.StageOptions)}.isdisjoint(
        {"input_dir", "out_dir", "only", "resume", "force"}
    )


def pathid_is_inside(path: Path, folder: Path) -> bool:
    from a2m import pathid

    return pathid.is_same_or_inside(path, folder)


def _r10_swap_stage(action):
    """A stage that runs ``action`` once, while alpha is processed (after discovery, before beta/gamma)."""
    def stage(proxy) -> None:
        if proxy.name == "alpha":
            action()
    return stage


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX links and FIFOs")
@pytest.mark.parametrize(
    "swap",
    [
        pytest.param("zip-to-link", id="CP1-T94-zip-swapped-for-link"),
        pytest.param("zip-to-fifo", id="CP1-T94-zip-swapped-for-fifo"),
        pytest.param("folder-to-link", id="CP1-T94-folder-swapped-for-link"),
        pytest.param("folder-to-fifo", id="CP1-T94-folder-swapped-for-fifo"),
        pytest.param("inner-file-to-link", id="CP1-T94-inner-file-swapped-for-link"),
        pytest.param("inner-folder-to-link", id="CP1-T94-inner-folder-swapped-for-link"),
    ],
)
def test_CP1_T94_an_input_item_swapped_after_discovery_is_refused_not_followed(
    tmp_path: Path, results_dir: Path, run_cli, swap: str
) -> None:
    """[CP1-T94] A2: a zip or folder swapped for a link or FIFO after discovery is refused; nothing hangs."""
    exports = tmp_path / "exports"
    exports.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    write_bundle_zip(other, "evil")
    write_bundle_dir(other, "evil")
    write_bundle_dir(exports, "alpha")
    write_bundle_zip(exports, "beta")
    write_bundle_dir(exports, "gamma")
    victim, target = {
        "zip-to-link": ("beta", exports / "beta.zip"),
        "zip-to-fifo": ("beta", exports / "beta.zip"),
        "folder-to-link": ("gamma", exports / "gamma"),
        "folder-to-fifo": ("gamma", exports / "gamma"),
        "inner-file-to-link": ("gamma", exports / "gamma" / "apiproxy" / "gamma.xml"),
        "inner-folder-to-link": ("gamma", exports / "gamma" / "apiproxy" / "proxies"),
    }[swap]

    def action() -> None:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        if swap.endswith("fifo"):
            os.mkfifo(target)
        elif swap.startswith("zip"):
            target.symlink_to(other / "evil.zip")
        elif swap == "inner-file-to-link":
            target.symlink_to(other / "evil" / "apiproxy" / "evil.xml")
        else:
            target.symlink_to(other / "evil" / ("apiproxy/proxies" if swap.startswith("inner") else ""))

    reader = _R10Reader()
    res = _in_thread(lambda: run_cli(base_argv(exports, results_dir), stages=[_r10_swap_stage(action), reader]))

    assert res.code == 0, res.err
    assert_no_traceback(res.err, res.out)
    assert victim not in reader.calls
    assert all("evil" not in text for text in reader.read.values())
    lines = log_lines(run_log(results_dir))
    assert any("ERROR" in line and f"refused {victim}" in line for line in lines), "\n".join(lines)
    assert "2 done" in res.out and "1 refused" in res.out and "0 failed" in res.out, res.out
    assert victim not in marker_names(results_dir)
    assert not (results_dir / ".a2m-work").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX links and FIFOs")
def test_CP1_T94_the_input_zip_opener_refuses_links_and_fifos_without_blocking(tmp_path: Path) -> None:
    """[CP1-T94] A2: extract_zip opens the zip through the one fd-based opener; link and FIFO raise, never block."""
    real = write_bundle_zip(tmp_path, "real")
    (tmp_path / "link.zip").symlink_to(real)
    os.mkfifo(tmp_path / "pipe.zip")
    for name in ("link.zip", "pipe.zip"):
        with pytest.raises(UnsafeBundleError):
            _in_thread(lambda name=name: a2m_discovery.extract_zip(tmp_path / name, tmp_path / f"out-{name}"))
        assert not any((tmp_path / f"out-{name}").iterdir())


def _r10_many_members_zip(path: Path, count: int) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(zipfile.ZipInfo("apiproxy/bomb.xml", (2026, 1, 1, 0, 0, 0)), "<APIProxy/>\n")
        for i in range(count - 1):
            zf.writestr(zipfile.ZipInfo(f"apiproxy/e/{i}", (2026, 1, 1, 0, 0, 0)), "")


def test_CP1_T95_a_zip_with_more_members_than_the_limit_is_refused_at_discovery(
    tmp_path: Path, results_dir: Path, run_cli, recorder
) -> None:
    """[CP1-T95] A3: MAX_MEMBERS + 1 empty members: refused at discovery, exit 0, nothing under .a2m-work."""
    exports = tmp_path / "exports"
    exports.mkdir()
    limit = a2m_discovery.MAX_MEMBERS
    assert limit == 10_000
    _r10_many_members_zip(exports / "bomb.zip", limit + 1)
    write_bundle_dir(exports, "gamma")

    res = run_cli(base_argv(exports, results_dir), stages=[recorder])

    assert res.code == 0, res.err
    lines = log_lines(run_log(results_dir))
    assert any("ERROR" in line and "refused bomb" in line and f"{limit + 1} members" in line for line in lines), (
        "\n".join(lines)
    )
    assert not any("processing bomb" in line for line in lines)
    assert recorder.calls == ["gamma"]
    assert not (results_dir / ".a2m-work").exists()
    assert "1 refused" in res.out

    with zipfile.ZipFile(exports / "bomb.zip") as zf:
        infos = zf.infolist()
    with pytest.raises(UnsafeBundleError, match="members"):
        check_zip_members(infos)


def _r10_zip64_declaring(path: Path, members: int, directory: int) -> None:
    """A small zip whose end records (zip64 form) declare ``members`` entries and a ``directory``-byte member list."""
    _write_raw_zip(path, [("apiproxy/x.xml", "<APIProxy/>\n")])
    data = path.read_bytes()
    at = data.rfind(b"PK\x05\x06")
    cd_offset = int.from_bytes(data[at + 16 : at + 20], "little")
    zip64_at = at
    record = (
        b"PK\x06\x06" + (44).to_bytes(8, "little") + (45).to_bytes(2, "little") + (45).to_bytes(2, "little")
        + (0).to_bytes(4, "little") + (0).to_bytes(4, "little") + members.to_bytes(8, "little")
        + members.to_bytes(8, "little") + directory.to_bytes(8, "little") + cd_offset.to_bytes(8, "little")
    )
    locator = b"PK\x06\x07" + (0).to_bytes(4, "little") + zip64_at.to_bytes(8, "little") + (1).to_bytes(4, "little")
    eocd = data[at:at + 8] + b"\xff\xff\xff\xff" + b"\xff\xff\xff\xff" + data[at + 16 :]
    path.write_bytes(data[:at] + record + locator + eocd)


@pytest.mark.parametrize(
    ("kind", "count", "size"),
    [
        pytest.param("plain", None, None, id="CP1-T95-declared-members"),
        pytest.param("zip64", 10_001, 100, id="CP1-T95-zip64-declared-members"),
        pytest.param("zip64", 3, 64 * 1024 * 1024, id="CP1-T95-zip64-declared-directory-size"),
    ],
)
def test_CP1_T95_the_member_list_is_checked_before_zipfile_loads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, count: int | None, size: int | None
) -> None:
    """[CP1-T95] A3: the end record's member count and directory size refuse a zip before ZipFile reads its list."""
    path = tmp_path / "bomb.zip"
    if kind == "plain":
        monkeypatch.setattr(a2m_discovery, "MAX_MEMBERS", 10)
        _r10_many_members_zip(path, 11)
    else:
        assert count is not None and size is not None
        _r10_zip64_declaring(path, count, size)

    def no_zipfile(*args, **kwargs):
        raise AssertionError("zipfile.ZipFile was reached")

    monkeypatch.setattr(a2m_discovery.zipfile, "ZipFile", no_zipfile)
    with pytest.raises(UnsafeBundleError):
        a2m_discovery._read_zip_infos(path)
    outcome = classify(path, "bomb")
    assert isinstance(outcome, RejectedItem) and "unsafe zip" in outcome.reason, outcome


@pytest.mark.parametrize(
    "limit",
    [
        pytest.param("members", id="CP1-T95-folder-members"),
        pytest.param("file-bytes", id="CP1-T95-folder-file-bytes"),
        pytest.param("total-bytes", id="CP1-T95-folder-total-bytes"),
        pytest.param("depth", id="CP1-T95-folder-depth"),
        pytest.param("zip-file-bytes", id="CP1-T95-zip-file-bytes"),
    ],
)
def test_CP1_T95_folder_and_zip_working_copies_share_the_limits(
    tmp_path: Path, results_dir: Path, run_cli, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    """[CP1-T95] A3: the folder copy has the zip limits (members, one file, total, depth); a breach is a refusal."""
    exports = tmp_path / "exports"
    exports.mkdir()
    alpha = write_bundle_dir(exports, "alpha")
    write_bundle_dir(exports, "gamma")
    if limit == "members":
        monkeypatch.setattr(a2m_discovery, "MAX_MEMBERS", 12)
        for i in range(10):
            (alpha / "apiproxy" / f"f{i}.txt").write_text("x", encoding="utf-8")
    elif limit == "file-bytes":
        monkeypatch.setattr(a2m_discovery, "MAX_FILE_BYTES", 5000)
        (alpha / "apiproxy" / "big.bin").write_bytes(b"x" * 5001)
    elif limit == "total-bytes":
        monkeypatch.setattr(a2m_discovery, "MAX_UNPACKED_BYTES", 6000)
        for i in range(3):
            (alpha / "apiproxy" / f"part{i}.bin").write_bytes(b"x" * 2500)
    elif limit == "depth":
        deep = alpha / "apiproxy" / "resources"
        for i in range(a2m_discovery.MAX_MEMBER_PARTS):
            deep = deep / f"d{i % 10}"
        deep.mkdir(parents=True)
    else:
        monkeypatch.setattr(a2m_discovery, "MAX_FILE_BYTES", 5000)
        shutil.rmtree(alpha)
        _write_raw_zip(exports / "alpha.zip", [("apiproxy/alpha.xml", "<APIProxy/>\n"), ("apiproxy/b.bin", "x" * 5001)])
    reader = _R10Reader()

    res = run_cli(base_argv(exports, results_dir), stages=[reader])

    assert res.code == 0, res.err
    assert reader.calls == ["gamma"]
    lines = log_lines(run_log(results_dir))
    assert any("ERROR" in line and "refused alpha" in line and "limit" in line for line in lines), "\n".join(lines)
    assert "1 refused" in res.out and "0 failed" in res.out
    assert marker_names(results_dir) == {"gamma"}
    assert not (results_dir / ".a2m-work").exists()


def _r10_flag_as_junction(monkeypatch: pytest.MonkeyPatch, *paths: Path) -> None:
    """Make safefs's reparse-point probe report each of ``paths`` (real folders) as a Windows junction."""
    ids = {(os.lstat(p).st_dev, os.lstat(p).st_ino) for p in paths}
    real = _r10_safefs._reparse_attributes

    def probe(info: os.stat_result) -> int:
        if (info.st_dev, info.st_ino) in ids:
            return real(info) | 0x400  # FILE_ATTRIBUTE_REPARSE_POINT
        return real(info)

    monkeypatch.setattr(_r10_safefs, "_reparse_attributes", probe)


def test_CP1_T96_every_guarded_operation_refuses_a_junction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[CP1-T96] A4: with .a2m-work and a proxy folder reported as junctions, every guarded change refuses."""
    root = tmp_path / "results"
    work = root / ".a2m-work"
    (work / "alpha").mkdir(parents=True)
    (work / "alpha" / "precious.txt").write_text("keep me\n", encoding="utf-8")
    proxy = root / "alpha"
    proxy.mkdir()
    (proxy / ".done").write_text("a2m finished alpha\n", encoding="utf-8")
    before = _tree_state(root)
    _r10_flag_as_junction(monkeypatch, work, proxy)
    rmtree_calls: list[object] = []
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: rmtree_calls.append(a))

    assert _r10_safefs.is_link(work) and _r10_safefs.is_link(proxy)
    assert not _r10_safefs.is_link(work / "alpha")
    for target in (work / "alpha", proxy / ".done"):
        with pytest.raises(UnsafePathError, match="junction"):
            _r10_safefs.remove(root, target)
        with pytest.raises(UnsafePathError):
            _r10_safefs.make_dirs(root, target / "sub")
        with pytest.raises(UnsafePathError):
            _r10_safefs.write_text_atomic(root, target.parent / "x.txt", "x")
        with pytest.raises(UnsafePathError):
            _r10_safefs.remove_empty_dir(root, target)
        with pytest.raises(UnsafePathError):
            _r10_safefs.open_plain_file(root, target.parent / "log", os.O_WRONLY | os.O_CREAT)
        assert _r10_safefs.is_regular_file(root, target) is False
    assert _r10_safefs.remove_empty_dir(root, work) is False
    try:
        _r10_safefs.remove(root, work)  # the junction itself: removed as a link, never emptied
    except OSError:
        pass
    assert rmtree_calls == []
    assert _tree_state(root) == before


def test_CP1_T96_a_junction_at_the_work_root_stops_the_run(
    tmp_path: Path, mixed_exports: Path, results_dir: Path, run_cli, recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP1-T96] A4: the up-front results check treats a junction at .a2m-work like a symbolic link (exit 2)."""
    assert run_cli(base_argv(mixed_exports, results_dir), stages=[recorder]).code == 0
    recorder.calls.clear()
    (results_dir / ".a2m-work").mkdir()
    (results_dir / ".a2m-work" / "beta").mkdir()
    (results_dir / ".a2m-work" / "beta" / "precious.txt").write_text("keep me\n", encoding="utf-8")
    _r10_flag_as_junction(monkeypatch, results_dir / ".a2m-work")
    before = _tree_state(results_dir)

    res = run_cli(base_argv(mixed_exports, results_dir, "--force"), stages=[recorder])

    assert res.code == 2, res.err
    assert ".a2m-work" in _one_stderr_line(res.err)
    assert recorder.calls == []
    assert _tree_state(results_dir) == before


def test_CP1_T96_junctions_in_the_input_are_refused(
    tmp_path: Path, results_dir: Path, run_cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP1-T96] A4: a junction as an input item, as apiproxy/, or deeper inside a folder bundle is refused."""
    exports = tmp_path / "exports"
    exports.mkdir()
    write_bundle_dir(exports, "item")
    named = write_bundle_dir(exports, "named")
    inner = write_bundle_dir(exports, "inner")
    (inner / "apiproxy" / "resources").mkdir()
    write_bundle_dir(exports, "gamma")
    _r10_flag_as_junction(monkeypatch, exports / "item", named / "apiproxy", inner / "apiproxy" / "resources")
    reader = _R10Reader()

    res = run_cli(base_argv(exports, results_dir), stages=[reader])

    assert res.code == 0, res.err
    assert reader.calls == ["gamma"]
    lines = log_lines(run_log(results_dir))
    for name, shown in (("item", "symbolic link"), ("named", "apiproxy"), ("inner", "apiproxy/resources")):
        assert any("ERROR" in line and f"refused {name}" in line and shown in line for line in lines), (
            name, "\n".join(lines)
        )
    assert "1 done" in res.out and "3 refused" in res.out


def test_CP1_T97_only_the_safefs_helper_tests_for_links() -> None:
    """[CP1-T97] A4 structural: no a2m module calls is_symlink, islink, S_ISLNK or junction tests outside safefs.is_link_like."""
    banned = {"is_symlink", "islink", "S_ISLNK", "is_junction", "isjunction"}
    problems: list[str] = []
    for path in _A2M_SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        allowed: set[int] = set()
        if path.name == "safefs.py":
            for fn in ast.walk(tree):
                if isinstance(fn, ast.FunctionDef) and fn.name == "is_link_like":
                    allowed = {id(n) for n in ast.walk(fn)}
        for node in ast.walk(tree):
            name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
            if name in banned and id(node) not in allowed:
                problems.append(f"{path.name}:{node.lineno} uses {name}; use safefs.is_link_like / is_link")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "is_dir"
                and any(k.arg == "follow_symlinks" for k in node.keywords)
            ):
                problems.append(f"{path.name}:{node.lineno} is_dir(follow_symlinks=False) counts a junction as a folder")
    assert problems == [], "\n".join(problems)


# ---------------------------------------------------------------- adversarial round 11 additions

from a2m.discovery import _NameKeys as _R11NameKeys  # noqa: E402
from a2m.discovery import _writing as _r11_writing  # noqa: E402
from a2m.discovery import copy_folder_bundle as _r11_copy_folder_bundle  # noqa: E402


def _r11_replace_zip(path: Path, members: list[tuple[str, str]]) -> None:
    """Atomically replace ``path`` with a valid zip holding ``members``."""
    staged = path.with_name(path.name + ".new")
    _write_raw_zip(staged, members)
    os.replace(staged, path)


_R11_PROXY = [("apiproxy/beta.xml", "<APIProxy/>\n"), ("apiproxy/proxies/default.xml", "<ProxyEndpoint/>\n")]


@pytest.mark.parametrize(
    ("swap", "victim", "expected", "found"),
    [
        pytest.param("zip-not-a-bundle", "beta", "apiproxy/", "no bundle", id="CP1-T98-zip-swapped-for-non-bundle"),
        pytest.param("zip-wrapped", "beta", "apiproxy/", "beta/apiproxy/", id="CP1-T98-zip-swapped-for-wrapped"),
        pytest.param("zip-unwrapped", "delta", "delta/apiproxy/", "apiproxy/", id="CP1-T98-wrapped-zip-unwrapped"),
        pytest.param("zip-shared-flow", "beta", "apiproxy/", "sharedflowbundle/", id="CP1-T98-zip-now-a-shared-flow"),
        pytest.param("folder-renamed", "gamma", "apiproxy/", "no bundle", id="CP1-T98-folder-apiproxy-renamed"),
        pytest.param("folder-wrapped", "gamma", "apiproxy/", "gamma/apiproxy/", id="CP1-T98-folder-now-wrapped"),
    ],
)
def test_CP1_T98_an_item_changed_after_discovery_is_refused_by_checking_the_copy(
    tmp_path: Path, results_dir: Path, run_cli, swap: str, victim: str, expected: str, found: str
) -> None:
    """[CP1-T98] A1: the working copy is classified again; a different zip or folder layout is refused, no .done."""
    exports = tmp_path / "exports"
    exports.mkdir()
    write_bundle_dir(exports, "alpha")
    write_bundle_zip(exports, "beta")
    write_bundle_dir(exports, "gamma")
    _write_raw_zip(exports / "delta.zip", [("delta/apiproxy/delta.xml", "<APIProxy/>\n")])
    originals = tmp_path / "originals"
    shutil.copytree(exports, originals)
    source_name = {"beta": "beta.zip", "delta": "delta.zip", "gamma": "gamma"}[victim]

    def action() -> None:
        if swap == "zip-not-a-bundle":
            _r11_replace_zip(exports / "beta.zip", [("docs/readme.txt", "not a bundle\n")])
        elif swap == "zip-wrapped":
            _r11_replace_zip(exports / "beta.zip", [(f"beta/{name}", text) for name, text in _R11_PROXY])
        elif swap == "zip-unwrapped":
            _r11_replace_zip(exports / "delta.zip", [("apiproxy/delta.xml", "<APIProxy/>\n")])
        elif swap == "zip-shared-flow":
            _r11_replace_zip(exports / "beta.zip", [("sharedflowbundle/beta.xml", "<SharedFlowBundle/>\n")])
        elif swap == "folder-renamed":
            (exports / "gamma" / "apiproxy").rename(exports / "gamma" / "docs")
        else:
            (exports / "gamma" / "apiproxy").rename(tmp_path / "moved")
            (exports / "gamma" / "gamma").mkdir()
            (tmp_path / "moved").rename(exports / "gamma" / "gamma" / "apiproxy")

    reader = _R10Reader()
    res = run_cli(base_argv(exports, results_dir), stages=[_r10_swap_stage(action), reader])

    assert res.code == 0, res.err
    assert_no_traceback(res.err, res.out)
    assert victim not in reader.calls
    assert sorted(reader.calls) == sorted({"alpha", "beta", "gamma", "delta"} - {victim})
    lines = log_lines(run_log(results_dir))
    message = f"refused {victim} ({source_name}): {source_name} changed after it was found: expected {expected} in it"
    assert any("ERROR" in line and message in line and f"found {found}" in line for line in lines), "\n".join(lines)
    assert "3 done" in res.out and "1 refused" in res.out and "0 failed" in res.out, res.out
    assert victim not in marker_names(results_dir)
    assert not (results_dir / "victim").exists()
    assert not (results_dir / ".a2m-work").exists()

    # With the original restored, --resume processes the refused proxy (it was never marked done).
    shutil.rmtree(exports)
    shutil.copytree(originals, exports)
    again = _R10Reader()
    res2 = run_cli(base_argv(exports, results_dir, "--resume"), stages=[again])
    assert res2.code == 0, res2.err
    assert again.calls == [victim], again.calls
    assert victim in marker_names(results_dir)


def test_CP1_T98_materialize_verifies_the_copy_with_the_discovery_rule(tmp_path: Path) -> None:
    """[CP1-T98] A1: _materialize refuses a copy whose bundle root differs from what discovery recorded."""
    from a2m.discovery import SHARED_FLOW_ROOT, SourceKind

    item = tmp_path / "flow"
    (item / "sharedflowbundle").mkdir(parents=True)
    (item / "sharedflowbundle" / "flow.xml").write_text("<SharedFlowBundle/>\n", encoding="utf-8")
    as_proxy = BundleSource("flow", item, SourceKind.FOLDER)
    with pytest.raises(UnsafeBundleError, match=r"flow changed after it was found: expected apiproxy/ in it, found sharedflowbundle/"):
        _r10_engine._materialize(as_proxy, tmp_path / "work1")
    wrapped = BundleSource("flow", item, SourceKind.FOLDER, SHARED_FLOW_ROOT, "flow")
    with pytest.raises(UnsafeBundleError, match=r"expected flow/sharedflowbundle/ in it, found sharedflowbundle/"):
        _r10_engine._materialize(wrapped, tmp_path / "work2")
    right = BundleSource("flow", item, SourceKind.FOLDER, SHARED_FLOW_ROOT, None)
    _r10_engine._materialize(right, tmp_path / "work3")
    assert (tmp_path / "work3" / "sharedflowbundle" / "flow.xml").is_file()


class _R11FoldingOs:
    """``os`` for a2m.discovery, but creating files and folders as a case-insensitive file system does."""

    def __getattr__(self, name: str):
        return getattr(os, name)

    @staticmethod
    def _clash(path: object) -> None:
        target = Path(os.fsdecode(path))  # type: ignore[arg-type]
        if target.parent.is_dir() and any(
            collision_key(child.name) == collision_key(target.name) for child in target.parent.iterdir()
        ):
            raise FileExistsError(errno.EEXIST, "File exists", str(target))

    def mkdir(self, path, mode: int = 0o777, *, dir_fd=None) -> None:
        if dir_fd is None:
            self._clash(path)
        os.mkdir(path, mode, dir_fd=dir_fd)

    def open(self, path, flags: int, mode: int = 0o777, *, dir_fd=None) -> int:
        if dir_fd is None and flags & os.O_CREAT:
            self._clash(path)
        return os.open(path, flags, mode, dir_fd=dir_fd)


def _r11_case_clash_folder(parent: Path, kind: str) -> Path:
    root = write_bundle_dir(parent, "alpha")
    policies = root / "apiproxy" / "policies"
    policies.mkdir(exist_ok=True)
    if kind == "files":
        (policies / "Quota.xml").write_text("<Quota name='A'/>\n", encoding="utf-8")
        (policies / "quota.xml").write_text("<Quota name='B'/>\n", encoding="utf-8")
    else:
        (root / "apiproxy" / "Policies").mkdir()
        (root / "apiproxy" / "Policies" / "x.xml").write_text("<X/>\n", encoding="utf-8")
        (policies / "y.xml").write_text("<Y/>\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("kind", [pytest.param("files", id="CP1-T99-files"), pytest.param("folders", id="CP1-T99-folders")])
@pytest.mark.parametrize("check", [pytest.param(True, id="CP1-T99-key-check"), pytest.param(False, id="CP1-T99-eexist")])
def test_CP1_T99_a_folder_with_names_differing_only_in_case_is_refused_like_its_zip_twin(
    tmp_path: Path, results_dir: Path, run_cli, monkeypatch: pytest.MonkeyPatch, kind: str, check: bool
) -> None:
    """[CP1-T99] A2: on a case-insensitive --out, a folder bundle whose names collide is refused (exit 0), never crashed.

    With the shared collision_key check on, it is refused before writing; with it off, the
    FileExistsError the file system raises is still a refusal, not a failure.
    """
    exports = tmp_path / "exports"
    exports.mkdir()
    src = _r11_case_clash_folder(exports, kind)
    write_bundle_dir(exports, "gamma")
    if check:  # the zip twin; with the shared check switched off it would not be checked at all
        shutil.make_archive(str(exports / "alphaz"), "zip", src)
    monkeypatch.setattr(a2m_discovery, "os", _R11FoldingOs())
    if not check:
        monkeypatch.setattr(_R11NameKeys, "key", lambda self, parts: tuple(parts))
    recorder = _R10Reader()

    res = run_cli(base_argv(exports, results_dir), stages=[recorder])

    assert res.code == 0, res.err
    assert_no_traceback(res.err, res.out)
    assert sorted(recorder.calls) == ["gamma"]
    lines = log_lines(run_log(results_dir))
    shown = "letter case" if check else "because of its name"
    assert any("ERROR" in line and "refused alpha (alpha)" in line and shown in line for line in lines), "\n".join(lines)
    if check:
        assert any("refused alphaz" in line and "letter case" in line for line in lines), "\n".join(lines)
    assert not any("failed alpha" in line for line in lines), "\n".join(lines)
    refused = "2 refused" if check else "1 refused"
    assert "1 done" in res.out and refused in res.out and "0 failed" in res.out, res.out
    assert marker_names(results_dir) == {"gamma"}
    assert not (results_dir / ".a2m-work").exists()


@pytest.mark.parametrize(
    "names",
    [
        pytest.param(("Quota.xml", "quota.xml"), id="CP1-T99-case"),
        pytest.param(("café.xml", "café.xml"), id="CP1-T99-unicode-normalization"),
        pytest.param(("x.xml", "x.xml."), id="CP1-T99-trailing-dot"),
    ],
)
def test_CP1_T99_folder_copy_and_zip_check_share_one_collision_rule(tmp_path: Path, names: tuple[str, str]) -> None:
    """[CP1-T99] A2: copy_folder_bundle and check_zip_members refuse the same colliding names with the same message."""
    src = tmp_path / "src"
    (src / "apiproxy").mkdir(parents=True)
    for name in names:
        (src / "apiproxy" / name).write_text("<X/>\n", encoding="utf-8")
    with pytest.raises(UnsafeBundleError) as folder_err:
        _r11_copy_folder_bundle(src, tmp_path / "copy")
    infos = [zipfile.ZipInfo(f"apiproxy/{name}", (2026, 1, 1, 0, 0, 0)) for name in sorted(names)]
    with pytest.raises(UnsafeBundleError) as zip_err:
        check_zip_members(infos)
    assert "differ only in letter case" in str(folder_err.value)
    assert str(folder_err.value) == str(zip_err.value)


@pytest.mark.parametrize(
    ("code", "refused"),
    [
        pytest.param(errno.EEXIST, True, id="CP1-T99-eexist"),
        pytest.param(errno.EISDIR, True, id="CP1-T99-eisdir"),
        pytest.param(errno.ENOTDIR, True, id="CP1-T99-enotdir"),
        pytest.param(errno.EINVAL, True, id="CP1-T99-einval"),
        pytest.param(errno.EILSEQ, True, id="CP1-T99-eilseq"),
        pytest.param(errno.ENAMETOOLONG, True, id="CP1-T99-enametoolong"),
        pytest.param(errno.ENOSPC, False, id="CP1-T99-enospc-still-fails"),
        pytest.param(errno.EACCES, False, id="CP1-T99-eacces-still-fails"),
    ],
)
def test_CP1_T99_name_errors_while_writing_the_copy_are_refusals(code: int, refused: bool) -> None:
    """[CP1-T99] A2: write errors the bundle's names cause are refusals; disk and permission errors still fail."""
    def fail() -> None:
        with _r11_writing("apiproxy/x.xml"):
            raise OSError(code, os.strerror(code), "x")

    if refused:
        with pytest.raises(UnsafeBundleError, match="apiproxy/x.xml"):
            fail()
    else:
        with pytest.raises(OSError) as err:
            fail()
        assert not isinstance(err.value, UnsafeBundleError)
        assert err.value.errno == code


# --- CP1 adversarial round 12: only the bundle root is copied; macOS metadata; the end record zipfile reads ---


class _R12Snapshot:
    """A stage that records, while each proxy runs, every path in its working copy and in its bundle_dir."""

    def __init__(self, results: Path) -> None:
        self.work_root = results / ".a2m-work"
        self.work: dict[str, list[str]] = {}
        self.bundle: dict[str, list[str]] = {}
        self.read: dict[str, str] = {}

    def __call__(self, proxy) -> None:
        work = self.work_root / proxy.name
        bundle = Path(proxy.bundle_dir)
        self.work[proxy.name] = sorted(p.relative_to(work).as_posix() for p in work.rglob("*"))
        self.bundle[proxy.name] = sorted(p.relative_to(bundle).as_posix() for p in bundle.rglob("*"))
        self.read[proxy.name] = "".join(p.read_text(encoding="utf-8", errors="replace") for p in work.rglob("*")
                                        if p.is_file() and not p.is_symlink())


def _r12_plant_beside(case: str, item: Path, secret: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Plant unsafe or oversized content BESIDE the bundle root in ``item``; return a name the log must list."""
    if case == "npm-bin-link":
        (item / "package.json").write_text('{"devDependencies": {"apigeelint": "*"}}\n', encoding="utf-8")
        (item / "node_modules" / "apigeelint").mkdir(parents=True)
        (item / "node_modules" / "apigeelint" / "cli.js").write_text("TOPSECRET\n", encoding="utf-8")
        (item / "node_modules" / ".bin").mkdir()
        (item / "node_modules" / ".bin" / "apigeelint").symlink_to("../apigeelint/cli.js")
        return "node_modules"
    if case == "link-outside":
        (item / "docs").symlink_to(secret)
        return "docs"
    if case == "dangling-link":
        (item / "latest").symlink_to(item / "missing")
        return "latest"
    if case == "fifo":
        os.mkfifo(item / "tool.sock")
        return "tool.sock"
    if case == "case-collision":
        (item / "README").write_text("TOPSECRET\n", encoding="utf-8")
        (item / "readme").write_text("TOPSECRET\n", encoding="utf-8")
        return "README"
    assert case == "over-member-limit"
    monkeypatch.setattr(a2m_discovery, "MAX_MEMBERS", 30)
    (item / ".git" / "objects").mkdir(parents=True)
    for i in range(40):
        (item / ".git" / "objects" / f"o{i}").write_text("TOPSECRET\n", encoding="utf-8")
    return ".git"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX links and FIFOs")
@pytest.mark.parametrize("wrapped", [pytest.param(False, id="top"), pytest.param(True, id="wrapped")])
@pytest.mark.parametrize(
    "case",
    [
        pytest.param("npm-bin-link", id="CP1-T100-node-modules-bin-link"),
        pytest.param("link-outside", id="CP1-T100-folder-link-outside"),
        pytest.param("dangling-link", id="CP1-T100-dangling-link"),
        pytest.param("fifo", id="CP1-T100-fifo"),
        pytest.param("case-collision", id="CP1-T100-case-collision"),
        pytest.param("over-member-limit", id="CP1-T100-sibling-over-member-limit"),
    ],
)
def test_CP1_T100_content_beside_the_bundle_root_never_refuses_a_folder_proxy(
    tmp_path: Path, results_dir: Path, run_cli, monkeypatch: pytest.MonkeyPatch, case: str, wrapped: bool
) -> None:
    """[CP1-T100] A1: links, FIFOs, collisions or limit breaches beside [wrapper/]apiproxy are left out, not refused."""
    exports = tmp_path / "exports"
    exports.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "key.txt").write_text("TOPSECRET\n", encoding="utf-8")
    if wrapped:
        item = exports / "orders"
        item.mkdir()
        write_bundle_dir(item, "orders")
        prefix = "orders/"
    else:
        item = write_bundle_dir(exports, "orders")
        prefix = ""
    named = _r12_plant_beside(case, item, secret, monkeypatch)
    write_bundle_dir(exports, "gamma")
    stage = _R12Snapshot(results_dir)

    res = run_cli(base_argv(exports, results_dir), stages=[stage])

    assert res.code == 0, res.err
    assert_no_traceback(res.err, res.out)
    assert sorted(stage.work) == ["gamma", "orders"]
    assert "2 done" in res.out and "0 refused" in res.out and "0 failed" in res.out, res.out
    assert marker_names(results_dir) == {"gamma", "orders"}
    expected_work = [p for p in (prefix.rstrip("/"),) if p] + [
        f"{prefix}{rel}"
        for rel in ("apiproxy", "apiproxy/orders.xml", "apiproxy/proxies", "apiproxy/proxies/default.xml")
    ]
    assert [p for p in stage.work["orders"] if not p.startswith(f"{prefix}apiproxy/targets")] == expected_work
    assert all(p == "apiproxy" or p.startswith("apiproxy/") for p in stage.bundle["orders"]), stage.bundle
    assert "TOPSECRET" not in stage.read["orders"]
    lines = log_lines(run_log(results_dir))
    note = f"orders: only {prefix}apiproxy/ is used; left out beside it:"
    assert any("INFO" in line and note in line and named in line for line in lines), "\n".join(lines)
    assert not any("ERROR" in line for line in lines), "\n".join(lines)
    assert not (results_dir / ".a2m-work").exists()


def test_CP1_T100_a_zip_proxy_unpacks_only_its_bundle_root(tmp_path: Path, results_dir: Path, run_cli) -> None:
    """[CP1-T100] A1: a zip's members beside [wrapper/]apiproxy are not unpacked; the run says what was left out."""
    exports = tmp_path / "exports"
    exports.mkdir()
    proxy = [("apiproxy/beta.xml", "<APIProxy/>\n"), ("apiproxy/proxies/default.xml", "<ProxyEndpoint/>\n")]
    extra = [("package.json", "{}\n"), ("node_modules/.bin/apigeelint", "TOPSECRET\n")]
    _write_raw_zip(exports / "beta.zip", proxy + extra)
    _write_raw_zip(exports / "delta.zip", [(f"delta/{n}", t) for n, t in proxy + extra] + [("notes.txt", "TOPSECRET")])
    stage = _R12Snapshot(results_dir)

    res = run_cli(base_argv(exports, results_dir), stages=[stage])

    assert res.code == 0, res.err
    assert "2 done" in res.out and "0 refused" in res.out, res.out
    assert stage.work["beta"] == ["apiproxy", "apiproxy/beta.xml", "apiproxy/proxies", "apiproxy/proxies/default.xml"]
    assert stage.work["delta"] == ["delta"] + [f"delta/{p}" for p in stage.work["beta"]]
    assert "TOPSECRET" not in stage.read["beta"] + stage.read["delta"]
    lines = log_lines(run_log(results_dir))
    assert any("beta: only apiproxy/ is used; left out beside it: node_modules, package.json" in line for line in lines)
    assert any(
        "delta: only delta/apiproxy/ is used; left out beside it: delta/node_modules, delta/package.json, notes.txt"
        in line for line in lines
    ), "\n".join(lines)


def test_CP1_T100_links_inside_the_bundle_root_are_still_refused(tmp_path: Path, results_dir: Path, run_cli) -> None:
    """[CP1-T100] A1: only content beside the bundle root is left out; a link inside apiproxy/ still refuses it."""
    exports = tmp_path / "exports"
    exports.mkdir()
    item = write_bundle_dir(exports, "orders")
    (item / "apiproxy" / "resources").symlink_to(tmp_path)
    (item / "node_modules").mkdir()
    write_bundle_dir(exports, "gamma")
    stage = _R12Snapshot(results_dir)

    res = run_cli(base_argv(exports, results_dir), stages=[stage])

    assert res.code == 0, res.err
    assert sorted(stage.work) == ["gamma"]
    lines = log_lines(run_log(results_dir))
    assert any("ERROR" in line and "refused orders" in line and "apiproxy/resources" in line for line in lines)
    assert marker_names(results_dir) == {"gamma"}


def test_CP1_T101_a_top_level_macosx_folder_is_skipped_not_a_proxy(
    tmp_path: Path, results_dir: Path, run_cli, recorder
) -> None:
    """[CP1-T101] A2: `unzip alpha.zip -d exports` leaves __MACOSX/alpha/apiproxy/._*; it is skipped, one proxy runs."""
    exports = tmp_path / "exports"
    exports.mkdir()
    write_bundle_dir(exports, "alpha")
    meta = exports / "__MACOSX" / "alpha" / "apiproxy"
    meta.mkdir(parents=True)
    (meta / "._alpha.xml").write_bytes(b"\x00\x05\x16\x07AppleDouble")
    (exports / "._alpha.zip").write_bytes(b"\x00\x05\x16\x07AppleDouble")
    (exports / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")

    res = run_cli(base_argv(exports, results_dir), stages=[recorder])

    assert res.code == 0, res.err
    assert recorder.calls == ["alpha"]
    assert "1 done" in res.out and "0 refused" in res.out and "0 failed" in res.out, res.out
    lines = log_lines(run_log(results_dir))
    for name in ("__MACOSX", "._alpha.zip", ".DS_Store"):
        assert any(f"skipped {name}: macOS archive metadata" in line for line in lines), (name, "\n".join(lines))
    assert not any("found proxy __MACOSX" in line or "ERROR" in line for line in lines), "\n".join(lines)
    assert not (results_dir / "__MACOSX").exists()
    assert marker_names(results_dir) == {"alpha"}


@pytest.mark.parametrize("kind", [pytest.param("folder", id="CP1-T101-folder"), pytest.param("zip", id="CP1-T101-zip")])
def test_CP1_T101_macosx_entries_never_count_as_a_wrapper_or_get_copied(
    tmp_path: Path, results_dir: Path, run_cli, kind: str
) -> None:
    """[CP1-T101] A2: alpha/apiproxy + __MACOSX/alpha/apiproxy is wrapper alpha; ._* and .DS_Store are not copied."""
    exports = tmp_path / "exports"
    exports.mkdir()
    members = [
        ("alpha/apiproxy/alpha.xml", "<APIProxy/>\n"),
        ("alpha/apiproxy/._alpha.xml", "AppleDouble"),
        ("alpha/apiproxy/.DS_Store", "Bud1"),
        ("alpha/.DS_Store", "Bud1"),
        ("__MACOSX/alpha/apiproxy/._alpha.xml", "AppleDouble"),
        ("__MACOSX/alpha/._apiproxy", "AppleDouble"),
    ]
    if kind == "zip":
        _write_raw_zip(exports / "item.zip", members)
        item = exports / "item.zip"
    else:
        item = exports / "item"
        for name, text in members:
            (item / name).parent.mkdir(parents=True, exist_ok=True)
            (item / name).write_text(text, encoding="utf-8")
    outcome = classify(item, "item")
    assert isinstance(outcome, BundleSource) and outcome.wrapper == "alpha" and outcome.root == "apiproxy", outcome
    stage = _R12Snapshot(results_dir)

    res = run_cli(base_argv(exports, results_dir), stages=[stage])

    assert res.code == 0, res.err
    assert "1 done" in res.out and "0 refused" in res.out, res.out
    assert stage.work["item"] == ["alpha", "alpha/apiproxy", "alpha/apiproxy/alpha.xml"]
    assert stage.bundle["item"] == ["apiproxy", "apiproxy/alpha.xml"]


def test_CP1_T101_one_predicate_decides_macos_metadata_structurally() -> None:
    """[CP1-T101] A2: is_macos_metadata accepts __MACOSX, .DS_Store and ._ names, and only those."""
    assert all(a2m_discovery.is_macos_metadata(n) for n in ("__MACOSX", ".DS_Store", "._alpha.xml", "._apiproxy"))
    assert not any(a2m_discovery.is_macos_metadata(n) for n in ("apiproxy", "alpha", ".git", "_MACOSX", "x._y"))


def _r12_end_record(data: bytes) -> int:
    at = len(data) - 22
    assert data[at : at + 4] == b"PK\x05\x06" and data[-2:] == b"\x00\x00"
    return at


def _r12_as_zip64(data: bytes, members: int | None = None, directory: int | None = None) -> bytes:
    """``data`` with a zip64 end record and locator before its end record (members/directory default to the truth)."""
    at = _r12_end_record(data)
    count = int.from_bytes(data[at + 10 : at + 12], "little")
    size = int.from_bytes(data[at + 12 : at + 16], "little")
    offset = int.from_bytes(data[at + 16 : at + 20], "little")
    members = count if members is None else members
    directory = size if directory is None else directory
    record = (
        b"PK\x06\x06" + (44).to_bytes(8, "little") + (45).to_bytes(2, "little") * 2 + (0).to_bytes(4, "little") * 2
        + members.to_bytes(8, "little") * 2 + directory.to_bytes(8, "little") + offset.to_bytes(8, "little")
    )
    locator = b"PK\x06\x07" + (0).to_bytes(4, "little") + at.to_bytes(8, "little") + (1).to_bytes(4, "little")
    return data[:at] + record + locator + data[at:]


def _r12_signature_in_entry_counts(data: bytes) -> bytes:
    """Write PK\\x05\\x06 into the end record's two entry-count fields (bytes 8-11), as finding A3 describes."""
    at = len(data) - 22
    return data[: at + 8] + b"PK\x05\x06" + data[at + 12 :]


@pytest.mark.parametrize(
    "case",
    [
        pytest.param("zip64-members", id="CP1-T102-zip64-over-member-limit-signature-in-counts"),
        pytest.param("directory", id="CP1-T102-directory-over-limit-signature-in-counts"),
        pytest.param("zip64-directory", id="CP1-T102-zip64-directory-over-limit-signature-in-counts"),
    ],
)
def test_CP1_T102_the_end_record_is_read_where_zipfile_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """[CP1-T102] A3: a signature planted in the end record's own fields no longer skips the pre-check."""
    path = tmp_path / "bomb.zip"
    if case == "zip64-members":
        count = a2m_discovery.MAX_MEMBERS + 2000
        _r10_many_members_zip(path, count)
        path.write_bytes(_r12_signature_in_entry_counts(_r12_as_zip64(path.read_bytes())))
        with zipfile.ZipFile(path) as zf:  # zipfile itself would load every member: the danger
            assert len(zf.infolist()) == count
        reason = f"has {count} members"
    else:
        _write_raw_zip(path, [("apiproxy/x.xml", "<APIProxy/>\n")])
        data = path.read_bytes()
        if case == "directory":
            at = _r12_end_record(data)
            data = data[: at + 12] + (64 * 1024 * 1024).to_bytes(4, "little") + data[at + 16 :]
        else:
            data = _r12_as_zip64(data, directory=64 * 1024 * 1024)
        path.write_bytes(_r12_signature_in_entry_counts(data))
        reason = "member list is 67108864 bytes"
    assert path.read_bytes().rfind(b"PK\x05\x06") == len(path.read_bytes()) - 22 + 8

    def no_zipfile(*args, **kwargs):
        raise AssertionError("zipfile.ZipFile was reached")

    monkeypatch.setattr(a2m_discovery.zipfile, "ZipFile", no_zipfile)
    with pytest.raises(UnsafeBundleError, match=reason):
        a2m_discovery._read_zip_infos(path)
    outcome = classify(path, "bomb")
    assert isinstance(outcome, RejectedItem) and outcome.reason.startswith("unsafe zip:") and reason in outcome.reason


def test_CP1_T102_lying_member_counts_are_still_refused_from_the_member_list(tmp_path: Path) -> None:
    """[CP1-T102] A3: 32-bit counts overwritten with the signature: the honest directory loads, then len() refuses."""
    path = tmp_path / "bomb.zip"
    count = a2m_discovery.MAX_MEMBERS + 2000
    _r10_many_members_zip(path, count)
    path.write_bytes(_r12_signature_in_entry_counts(path.read_bytes()))
    outcome = classify(path, "bomb")
    assert isinstance(outcome, RejectedItem) and f"unsafe zip: has {count} members" in outcome.reason, outcome


def test_CP1_T102_a_zip_over_the_size_cap_is_refused_before_zipfile_opens_it(
    tmp_path: Path, results_dir: Path, run_cli, monkeypatch: pytest.MonkeyPatch, recorder
) -> None:
    """[CP1-T102] A3: a zip larger than MAX_ZIP_BYTES is refused from fstat, before zipfile reads anything."""
    assert a2m_discovery.MAX_ZIP_BYTES == 512 * 1024 * 1024
    exports = tmp_path / "exports"
    exports.mkdir()
    big = write_bundle_zip(exports, "big")
    write_bundle_dir(exports, "gamma")
    monkeypatch.setattr(a2m_discovery, "MAX_ZIP_BYTES", big.stat().st_size - 1)
    real_zipfile = a2m_discovery.zipfile.ZipFile
    opened: list[object] = []

    def watch(fh, *args, **kwargs):
        opened.append(fh)
        return real_zipfile(fh, *args, **kwargs)

    monkeypatch.setattr(a2m_discovery.zipfile, "ZipFile", watch)
    outcome = classify(big, "big")
    assert isinstance(outcome, RejectedItem), outcome
    assert f"unsafe zip: is {big.stat().st_size} bytes, more than the" in outcome.reason
    assert opened == []

    res = run_cli(base_argv(exports, results_dir), stages=[recorder])
    assert res.code == 0, res.err
    assert recorder.calls == ["gamma"] and "1 refused" in res.out


# --- CP1 adversarial round 13: the pre-check uses zipfile's own end-record reader; one meaning of bundle root ---

_R13_EOCD = b"PK\x05\x06"


def _r13_zip_with_trailing(path: Path, trailing: int, members: int = 11, directory: int | None = None) -> int:
    """An ``members``-member zip followed by ``trailing`` zero bytes; return where its end record starts.

    ``directory`` overwrites the 32-bit declared directory size. The zero bytes
    hold no end-record signature, so the last signature in the file is the record.
    """
    _r10_many_members_zip(path, members)
    data = path.read_bytes()
    at = len(data) - 22
    assert data[at : at + 4] == _R13_EOCD
    if directory is not None:
        data = data[: at + 12] + directory.to_bytes(4, "little") + data[at + 16 :]
    path.write_bytes(data + b"\0" * trailing)
    return at


def _r13_zipfile_count(path: Path) -> int | None:
    """How many members the real zipfile.ZipFile loads from ``path``, or None when it refuses the file."""
    try:
        with zipfile.ZipFile(path) as zf:
            return len(zf.infolist())
    except zipfile.BadZipFile:
        return None


@pytest.mark.parametrize("trailing", [65_535, 65_536], ids=["CP1-T103-65535-trailing", "CP1-T103-65536-trailing"])
@pytest.mark.parametrize("case", ["members", "directory"], ids=["CP1-T103-members", "CP1-T103-directory-64MiB"])
def test_CP1_T103_an_end_record_at_the_edge_of_zipfiles_window_is_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, trailing: int
) -> None:
    """[CP1-T103] A1: with 65,536 trailing bytes (record at size - 65558) the pre-check still refuses first."""
    path = tmp_path / "bomb.zip"
    directory = 64 * 1024 * 1024 if case == "directory" else None
    at = _r13_zip_with_trailing(path, trailing, directory=directory)
    size = path.stat().st_size
    assert at == size - 22 - trailing
    if trailing == 65_536:
        assert at == size - 65_558  # zipfile's maxCommentStart: the first byte zipfile searches from
    if case == "members":
        assert _r13_zipfile_count(path) == 11  # zipfile itself finds this record and loads every member
        monkeypatch.setattr(a2m_discovery, "MAX_MEMBERS", 10)
        reason = "has 11 members"
    else:
        reason = "member list is 67108864 bytes"

    def no_zipfile(*args, **kwargs):
        raise AssertionError("zipfile.ZipFile was reached")

    monkeypatch.setattr(a2m_discovery.zipfile, "ZipFile", no_zipfile)
    with pytest.raises(UnsafeBundleError, match=reason):
        a2m_discovery._read_zip_infos(path)
    outcome = classify(path, "bomb")
    assert isinstance(outcome, RejectedItem) and outcome.reason.startswith("unsafe zip:") and reason in outcome.reason


@pytest.mark.parametrize("limit", [None, 10], ids=["CP1-T103-sweep-default-limit", "CP1-T103-sweep-limit-10"])
def test_CP1_T103_the_pre_check_agrees_with_zipfile_over_a_sweep_of_trailing_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: int | None
) -> None:
    """[CP1-T103] A1: for 65,500-65,600 trailing bytes the pre-check and zipfile.ZipFile agree on every file.

    Whenever zipfile would load the zip, the pre-check knows its member count:
    it refuses 11 members over a limit of 10 and passes them under the default.
    Whenever zipfile refuses the file, the pre-check refuses it too.
    """
    if limit is not None:
        monkeypatch.setattr(a2m_discovery, "MAX_MEMBERS", limit)
    loaded: list[int] = []
    refused: list[int] = []
    for trailing in range(65_500, 65_601):
        path = tmp_path / f"t{trailing}.zip"
        _r13_zip_with_trailing(path, trailing)
        count = _r13_zipfile_count(path)
        if count is None:
            refused.append(trailing)
            with pytest.raises((UnsafeBundleError, zipfile.BadZipFile)):
                a2m_discovery._open_input_zip(path).close()
            outcome = classify(path, "t")
            assert isinstance(outcome, RejectedItem), (trailing, outcome)
            continue
        loaded.append(trailing)
        assert count == 11, trailing
        if limit is None:
            a2m_discovery._open_input_zip(path).close()
            assert a2m_discovery._read_zip_infos(path) and len(a2m_discovery._read_zip_infos(path)) == 11
        else:
            with pytest.raises(UnsafeBundleError, match="has 11 members"):
                a2m_discovery._open_input_zip(path).close()
    # The sweep really crosses zipfile's window edge: 65,536 trailing bytes load, 65,537 do not.
    assert loaded == list(range(65_500, 65_537)) and refused == list(range(65_537, 65_601)), (loaded, refused)


def test_CP1_T103_a_file_zipfile_finds_no_end_record_in_is_refused_in_zipfiles_words(tmp_path: Path) -> None:
    """[CP1-T103] A1: no end record at all: refused as zipfile would refuse it, with zipfile's own message."""
    path = tmp_path / "corrupt.zip"
    path.write_bytes(b"not a zip")
    with pytest.raises(zipfile.BadZipFile) as real:
        zipfile.ZipFile(path)
    outcome = classify(path, "corrupt")
    assert isinstance(outcome, RejectedItem), outcome
    assert outcome.reason == f"not a readable zip file ({real.value})", outcome.reason


def test_CP1_T103_the_end_record_is_found_only_by_zipfiles_own_reader() -> None:
    """[CP1-T103] A1 structural: no hand-written end-record search is left; the pre-check calls zipfile's reader."""
    assert a2m_discovery._zip_end_record is zipfile._EndRecData  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "layout",
    ["folder", "folder-wrapped", "zip", "zip-wrapped"],
    ids=["CP1-T104-folder", "CP1-T104-folder-wrapped", "CP1-T104-zip", "CP1-T104-zip-wrapped"],
)
def test_CP1_T104_bundle_dir_is_the_folder_that_holds_the_bundle_root(
    tmp_path: Path, results_dir: Path, run_cli, layout: str
) -> None:
    """[CP1-T104] A2: bundle_dir holds the bundle root, so a stage finds the proxy at bundle_dir / PROXY_ROOT."""
    exports = tmp_path / "exports"
    exports.mkdir()
    wrapped = layout.endswith("wrapped")
    if layout.startswith("folder"):
        write_bundle_dir(exports / "alpha" if wrapped else exports, "alpha")
    else:
        prefix = "alpha/" if wrapped else ""
        _write_raw_zip(exports / "alpha.zip", [(f"{prefix}apiproxy/alpha.xml", "<APIProxy/>\n")])
    seen: dict[str, object] = {}

    def stage(proxy) -> None:
        bundle = Path(proxy.bundle_dir)
        seen["holds_root"] = (bundle / a2m_discovery.PROXY_ROOT).is_dir()
        seen["name"] = bundle.name

    res = run_cli(base_argv(exports, results_dir), stages=[stage])

    assert res.code == 0, res.err
    assert seen["holds_root"] is True, seen
    assert seen["name"] != a2m_discovery.PROXY_ROOT, seen
    found = [line for line in log_lines(run_log(results_dir)) if "found proxy alpha" in line]
    assert len(found) == 1, found
    if wrapped:
        assert found[0].endswith(", bundle root found inside its top folder alpha/)"), found
    assert "used as the bundle root" not in run_log(results_dir)


# ---------------------------------------------------------------- CP1 adversarial round 14 additions
# New imports for this block live here so no existing line changes.
import argparse as _r14_argparse  # noqa: E402
import os as _r14_os  # noqa: E402

from a2m.cli import build_parser as _r14_build_parser  # noqa: E402


def _r14_tree(folder: Path) -> list[str]:
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*"))


@pytest.mark.parametrize(
    ("which", "value"),
    [
        ("EXPORTS", ""),
        ("EXPORTS", "   "),
        ("--out", ""),
        ("--out", " \t "),
        ("--golden", ""),
        ("--golden", "  "),
    ],
    ids=[
        "CP1-T105-empty-exports",
        "CP1-T105-blank-exports",
        "CP1-T105-empty-out",
        "CP1-T105-blank-out",
        "CP1-T105-empty-golden",
        "CP1-T105-blank-golden",
    ],
)
def test_CP1_T105_an_empty_or_blank_path_argument_is_a_usage_error(
    tmp_path: Path, mixed_exports: Path, results_dir: Path, run_cli, monkeypatch: pytest.MonkeyPatch,
    which: str, value: str,
) -> None:
    """[CP1-T105] A1: '' or whitespace for EXPORTS, --out or --golden exits 2 with one line and writes nothing."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    # A bundle in the cwd: an empty EXPORTS read as '.' would migrate it.
    write_bundle_dir(cwd, "alpha")
    monkeypatch.chdir(cwd)
    before = _r14_tree(cwd)
    exports = value if which == "EXPORTS" else str(mixed_exports)
    out = value if which == "--out" else str(results_dir)
    argv = ["migrate", exports, "--out", out, "--llm", "fake", "--no-runtime"]
    if which == "--golden":
        argv += ["--golden", value]

    res = run_cli(argv)

    assert res.code == 2, (res.code, res.out, res.err)
    lines = res.err.splitlines()
    assert len(lines) == 1, res.err
    assert "usage error" in lines[0] and which in lines[0] and "expected a path" in lines[0], lines[0]
    assert res.out == ""
    assert _r14_tree(cwd) == before
    assert not results_dir.exists()


def test_CP1_T105_every_path_argument_refuses_an_empty_value() -> None:
    """[CP1-T105] A1 class-wide: any migrate argument whose type yields a Path refuses '' and blanks."""
    parser = _r14_build_parser()
    subparsers = next(a for a in parser._actions if isinstance(a, _r14_argparse._SubParsersAction))
    migrate = subparsers.choices["migrate"]
    path_args = []
    for action in migrate._actions:
        convert = action.type
        if not callable(convert):
            continue
        try:
            sample = convert("somewhere")
        except (TypeError, ValueError, _r14_argparse.ArgumentTypeError):
            continue
        if not isinstance(sample, Path):
            continue
        path_args.append(action.dest)
        for bad in ("", " ", "\t"):
            with pytest.raises(_r14_argparse.ArgumentTypeError):
                convert(bad)
    assert {"exports", "out", "golden"} <= set(path_args), path_args


def test_CP1_T105_an_unset_variable_through_the_console_script_is_refused(
    tmp_path: Path, subprocess_env: dict[str, str]
) -> None:
    """[CP1-T105] A1: `a2m migrate "$EXPORTS" --out "$RESULTS"` with both unset exits 2 and writes nothing."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    proc = subprocess.run(
        [str(_VENV_BIN / "a2m"), "migrate", "", "--out", "", "--llm", "fake", "--no-runtime"],
        cwd=cwd, env=subprocess_env, capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert len(proc.stderr.splitlines()) == 1, proc.stderr
    assert "expected a path" in proc.stderr
    assert list(cwd.iterdir()) == []


@pytest.mark.parametrize(
    "root",
    ["apiproxy", "sharedflowbundle"],
    ids=["CP1-T106-proxy-bundle", "CP1-T106-shared-flow-bundle"],
)
def test_CP1_T106_exports_that_is_itself_a_bundle_says_pass_the_parent(
    tmp_path: Path, results_dir: Path, run_cli, root: str
) -> None:
    """[CP1-T106] A2: EXPORTS with apiproxy/ or sharedflowbundle/ at its top exits 2 naming the parent folder."""
    bundle = tmp_path / "exports" / "alpha"
    (bundle / root).mkdir(parents=True)
    (bundle / root / "alpha.xml").write_text("<APIProxy/>\n", encoding="utf-8")

    res = run_cli(base_argv(bundle, results_dir))

    assert res.code == 2, (res.code, res.out, res.err)
    lines = res.err.splitlines()
    assert len(lines) == 1, res.err
    assert "usage error" in lines[0], lines[0]
    assert f"is itself a bundle (it has {root}/ at its top)" in lines[0], lines[0]
    assert "pass the folder that contains it" in lines[0], lines[0]
    assert str(bundle.parent) in lines[0], lines[0]
    assert "no proxies found" not in lines[0], lines[0]
    assert not results_dir.exists()


def test_CP1_T106_the_bundle_message_wins_over_a_results_folder_problem(
    tmp_path: Path, results_dir: Path, run_cli
) -> None:
    """[CP1-T106] A2: the real cause is reported first, even when --out is a used non-a2m folder."""
    bundle = write_bundle_dir(tmp_path / "exports", "alpha")
    results_dir.mkdir()
    (results_dir / "keep.txt").write_text("mine\n", encoding="utf-8")

    res = run_cli(base_argv(bundle, results_dir))

    assert res.code == 2, res.err
    assert "pass the folder that contains it" in res.err, res.err
    assert sorted(p.name for p in results_dir.iterdir()) == ["keep.txt"]


@pytest.mark.skipif(not hasattr(_r14_os, "symlink"), reason="needs symlinks")
def test_CP1_T106_a_link_named_apiproxy_is_not_followed(tmp_path: Path, results_dir: Path, run_cli) -> None:
    """[CP1-T106] A2 guard: a symlink named apiproxy is not followed; it is refused as a link, as before."""
    real = tmp_path / "elsewhere" / "apiproxy"
    real.mkdir(parents=True)
    exports = tmp_path / "exports"
    exports.mkdir()
    try:
        (exports / "apiproxy").symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symlinks here")

    res = run_cli(base_argv(exports, results_dir))

    assert "is itself a bundle" not in res.err + res.out, (res.out, res.err)
    assert res.code == 0, res.err
    assert "1 refused" in res.out, res.out
    assert "symbolic link" in run_log(results_dir)


def test_CP1_T106_a_folder_of_bundles_next_to_a_stray_apiproxy_still_runs(
    tmp_path: Path, results_dir: Path, run_cli
) -> None:
    """[CP1-T106] A2 guard: the check applies only when nothing was found, so a normal batch is unchanged."""
    exports = tmp_path / "exports"
    write_bundle_dir(exports, "alpha")
    (exports / "apiproxy").mkdir()

    res = run_cli(base_argv(exports, results_dir))

    assert res.code == 0, res.err
    assert "alpha" in marker_names(results_dir)


# ---------------------------------------------------------------- CP1 adversarial round 14, finding A3
# Behaviour tests that replace source-text pins (approved by Rahil, option A).


@pytest.mark.parametrize(
    "layout",
    ["folder", "folder-wrapped", "zip", "zip-wrapped"],
    ids=["CP1-T107-folder", "CP1-T107-folder-wrapped", "CP1-T107-zip", "CP1-T107-zip-wrapped"],
)
def test_CP1_T107_stages_see_only_a_copy_under_the_work_area_for_every_layout(
    tmp_path: Path, results_dir: Path, run_cli, layout: str
) -> None:
    """[CP1-T107] A3: bundle_dir is a copy under out/.a2m-work; changing EXPORTS during a stage does not change it."""
    exports = tmp_path / "exports"
    exports.mkdir()
    wrapped = layout.endswith("wrapped")
    original = '<APIProxy name="alpha"/>\n'
    if layout.startswith("folder"):
        root = exports / "alpha" / "alpha" if wrapped else exports / "alpha"
        (root / "apiproxy").mkdir(parents=True)
        source_file = root / "apiproxy" / "alpha.xml"
        source_file.write_text(original, encoding="utf-8")

        def change_exports() -> None:
            source_file.write_text("CHANGED\n", encoding="utf-8")  # in place, so a hard link would show it
            (root / "apiproxy" / "added.xml").write_text("ADDED\n", encoding="utf-8")
    else:
        prefix = "alpha/" if wrapped else ""
        item = exports / "alpha.zip"
        _write_raw_zip(item, [(f"{prefix}apiproxy/alpha.xml", original)])

        def change_exports() -> None:
            _write_raw_zip(item, [(f"{prefix}apiproxy/alpha.xml", "CHANGED\n"), (f"{prefix}apiproxy/added.xml", "x")])

    seen: dict[str, object] = {}

    def files(bundle: Path) -> dict[str, str]:
        return {
            p.relative_to(bundle).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(bundle.rglob("*")) if p.is_file()
        }

    def stage(proxy) -> None:
        bundle = Path(proxy.bundle_dir)
        seen["bundle"] = bundle.resolve()
        seen["paths"] = _r10_path_values(proxy)
        seen["before"] = files(bundle)
        change_exports()
        seen["after"] = files(bundle)
        seen["links"] = [p for p in bundle.rglob("*") if p.is_symlink()]

    res = run_cli(base_argv(exports, results_dir), stages=[stage])

    assert res.code == 0, res.err
    bundle = seen["bundle"]
    assert isinstance(bundle, Path)
    assert bundle.is_relative_to((results_dir / ".a2m-work").resolve()), bundle
    assert not pathid_is_inside(bundle, exports), bundle
    paths = seen["paths"]
    assert isinstance(paths, list) and paths, paths
    for path in paths:
        assert not pathid_is_inside(path, exports), path
    assert seen["before"] == {"apiproxy/alpha.xml": original}, seen["before"]
    assert seen["after"] == seen["before"], seen["after"]
    assert seen["links"] == []


def test_CP1_T108_an_item_whose_classification_raises_is_refused_under_its_item_name(
    tmp_path: Path, results_dir: Path, monkeypatch: pytest.MonkeyPatch, recorder
) -> None:
    """[CP1-T108] A3: when reading an item raises, only it is refused, named by item_name; the rest finish."""
    exports = tmp_path / "exports"
    exports.mkdir()
    write_bundle_dir(exports, "gamma")
    write_bundle_zip(exports, "beta")
    write_bundle_zip(exports, "Alpha.Beta").rename(exports / "Alpha.Beta.ZIP")
    write_bundle_zip(exports, "Ünïcödé.v2")
    failing = {"Alpha.Beta.ZIP", "Ünïcödé.v2.zip"}
    real = a2m_discovery._classify

    def flaky(path: Path, name: str):
        if path.name in failing:
            raise RuntimeError(f"boom reading {path.name}")
        return real(path, name)

    monkeypatch.setattr(a2m_discovery, "_classify", flaky)
    names = {file: a2m_discovery.item_name(exports / file) for file in failing}
    assert names == {"Alpha.Beta.ZIP": "Alpha.Beta", "Ünïcödé.v2.zip": "Ünïcödé.v2"}

    plan = prepare_run(RunOptions(input_dir=exports, out_dir=results_dir, llm=A2mLlmChoice.FAKE, no_runtime=True))
    result = run_batch(plan, [recorder])

    assert sorted(result.refused) == sorted(names.values()), result
    assert sorted(result.finished) == ["beta", "gamma"] and result.crashed == [], result
    assert sorted(recorder.calls) == ["beta", "gamma"]
    refused = _r9_refused_lines(results_dir)
    assert len(refused) == 2, refused
    for file, name in names.items():
        mine = [line for line in refused if f"refused {name} ({file}): " in line]
        assert len(mine) == 1, (file, refused)
        assert f"RuntimeError: boom reading {file}" in mine[0], mine[0]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX links and FIFOs")
def test_CP1_T109_links_and_junctions_are_refused_without_ever_reading_what_they_lead_to(
    tmp_path: Path, results_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP1-T109] A3: links and junctions at the top, as apiproxy and deeper are refused; the FIFO behind them is never opened."""
    exports = tmp_path / "exports"
    exports.mkdir()
    fifo = tmp_path / "secret.pipe"
    os.mkfifo(fifo)
    write_bundle_dir(exports, "gamma")
    (exports / "top").symlink_to(fifo)
    (exports / "named").mkdir()
    (exports / "named" / "apiproxy").symlink_to(fifo)
    inside = write_bundle_dir(exports, "inside")
    (inside / "apiproxy" / "policies").mkdir()
    (inside / "apiproxy" / "policies" / "Key.xml").symlink_to(fifo)
    jtop = write_bundle_dir(exports, "jtop")
    jnamed = write_bundle_dir(exports, "jnamed")
    jinner = write_bundle_dir(exports, "jinner")
    (jinner / "apiproxy" / "resources").mkdir()
    for folder in (jtop / "apiproxy", jnamed / "apiproxy", jinner / "apiproxy" / "resources"):
        os.mkfifo(folder / "behind.pipe")  # following the junction and copying it would block here
    _r10_flag_as_junction(monkeypatch, jtop, jnamed / "apiproxy", jinner / "apiproxy" / "resources")
    reader = _R10Reader()

    code = _in_thread(lambda: a2m_main(base_argv(exports, results_dir), stages=[reader]))

    assert code == 0
    assert reader.calls == ["gamma"]
    refused = _r9_refused_lines(results_dir)
    assert len(refused) == 6, refused
    for name, words in (
        ("top", f"symbolic link to {fifo}"),
        ("named", "apiproxy is a symbolic link"),
        ("inside", "apiproxy/policies/Key.xml is a symbolic link or junction"),
        ("jtop", "symbolic link"),
        ("jnamed", "apiproxy is a symbolic link"),
        ("jinner", "apiproxy/resources is a symbolic link or junction"),
    ):
        mine = [line for line in refused if f"refused {name} ({name}): " in line]
        assert len(mine) == 1, (name, refused)
        assert words in mine[0] and "does not follow links" in mine[0], mine[0]
    assert stat.S_ISFIFO(os.lstat(fifo).st_mode)
    assert not (results_dir / ".a2m-work").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX links and FIFOs")
def test_CP1_T110_discovery_refuses_a_zip_link_or_fifo_without_blocking(tmp_path: Path) -> None:
    """[CP1-T110] A3: classify and discover refuse link.zip and pipe.zip and return; neither is opened."""
    exports = tmp_path / "exports"
    exports.mkdir()
    real = write_bundle_zip(tmp_path, "real")
    (exports / "link.zip").symlink_to(real)
    os.mkfifo(exports / "pipe.zip")
    for file in ("link.zip", "pipe.zip"):
        path = exports / file
        outcome = _in_thread(lambda path=path: classify(path, a2m_discovery.item_name(path)))
        assert isinstance(outcome, RejectedItem), outcome
    found = _in_thread(lambda: a2m_discovery.discover(exports))
    assert found.proxies == () and found.skipped == ()
    assert sorted(item.path.name for item in found.rejected) == ["link.zip", "pipe.zip"]


# ---------------------------------------------------------------- CP1 adversarial round 15, finding A1
# --force clears every selected proxy's .done before redoing any, and interrupt advice names the flags
# that finish the job. New imports for the cases below live here so no existing line changes.

from a2m import engine as _r15_engine  # noqa: E402
from a2m import safefs as _r15_safefs  # noqa: E402


def _r15_versioned(version: str, calls: list[str], interrupt_in: str | None = None, *, kill: bool = False):
    """A stage that writes out.txt = version, and stops in ``interrupt_in`` (Ctrl-C, or a kill-like BaseException)."""

    def stage(proxy) -> None:
        calls.append(proxy.name)
        if proxy.name == interrupt_in:
            raise (_R15Killed() if kill else KeyboardInterrupt())
        out_dir = Path(proxy.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "out.txt").write_text(version, encoding="utf-8")

    return stage


class _R15Killed(BaseException):
    """Stands in for a kill or SIGHUP: nothing in a2m catches it and no message is printed."""


def _r15_outputs(results: Path) -> dict[str, str]:
    return {name: (results / name / "out.txt").read_text(encoding="utf-8") for name in sorted(ALL3)}


def _r15_finish_v1(exports: Path, results: Path, run_cli) -> None:
    calls: list[str] = []
    res = run_cli(base_argv(exports, results), stages=[_r15_versioned("v1", calls)])
    assert res.code == 0, res.err
    assert marker_names(results) == ALL3 and _r15_outputs(results) == dict.fromkeys(ALL3, "v1")


def test_CP1_T111_an_interrupted_force_run_then_resume_redoes_everything_it_had_not_finished(
    mixed_exports: Path, results_dir: Path, run_cli
) -> None:
    """[CP1-T111] A1 repro: v1 run, --force v2 interrupted in beta, then --resume v2 redoes beta and gamma; all v2."""
    _r15_finish_v1(mixed_exports, results_dir, run_cli)

    calls: list[str] = []
    res = run_cli(base_argv(mixed_exports, results_dir, "--force"), stages=[_r15_versioned("v2", calls, "beta")])
    assert res.code == 130, (res.out, res.err)
    line = _one_stderr_line(res.err)
    assert "interrupted" in line and "rerun with --resume" in line and "--force" not in line, line
    assert calls == ["alpha", "beta"]
    assert marker_names(results_dir) == {"alpha"}, "gamma kept the .done of the earlier run"
    entries = [e for e in log_lines(run_log(results_dir)) if "run interrupted while processing beta" in e]
    assert len(entries) == 1 and "rerun with --resume" in entries[0], entries

    calls.clear()
    res = run_cli(base_argv(mixed_exports, results_dir, "--resume"), stages=[_r15_versioned("v2", calls)])
    assert res.code == 0, res.err
    assert calls == ["beta", "gamma"]
    assert "2 done, 1 skipped as already done" in res.out, res.out
    assert marker_names(results_dir) == ALL3
    assert _r15_outputs(results_dir) == dict.fromkeys(ALL3, "v2")


def test_CP1_T111_a_killed_force_run_then_resume_keeps_no_stale_output(
    mixed_exports: Path, results_dir: Path, run_cli
) -> None:
    """[CP1-T111] A1: a --force run stopped by a kill (no Ctrl-C handling at all) still leaves no stale .done."""
    _r15_finish_v1(mixed_exports, results_dir, run_cli)

    calls: list[str] = []
    with pytest.raises(_R15Killed):
        run_cli(base_argv(mixed_exports, results_dir, "--force"), stages=[_r15_versioned("v2", calls, "beta", kill=True)])
    assert calls == ["alpha", "beta"]
    assert marker_names(results_dir) == {"alpha"}

    calls.clear()
    res = run_cli(base_argv(mixed_exports, results_dir, "--resume"), stages=[_r15_versioned("v2", calls)])
    assert res.code == 0, res.err
    assert calls == ["beta", "gamma"]
    assert _r15_outputs(results_dir) == dict.fromkeys(ALL3, "v2")


@pytest.mark.parametrize("flags", [("--force",), ()], ids=["CP1-T111-force", "CP1-T111-fresh"])
def test_CP1_T111_an_interrupt_during_discovery_names_the_flag_that_finishes_the_job(
    mixed_exports: Path, results_dir: Path, run_cli, monkeypatch: pytest.MonkeyPatch, flags: tuple[str, ...]
) -> None:
    """[CP1-T111] A1: Ctrl-C while a --force run discovers says rerun with --force; a plain run keeps --resume."""
    _r15_finish_v1(mixed_exports, results_dir, run_cli)
    before = snapshot(results_dir)

    def interrupted(_input_dir: Path):
        raise KeyboardInterrupt

    monkeypatch.setattr(_r15_engine, "discover", interrupted)
    res = run_cli(base_argv(mixed_exports, results_dir, *flags), stages=[_r15_versioned("v2", [])])

    assert res.code == 130, (res.out, res.err)
    line = _one_stderr_line(res.err)
    assert_no_traceback(res.err, res.out)
    if flags:
        assert "rerun with --force" in line and "--resume" not in line, line
    else:
        assert "rerun with --resume" in line and "--force" not in line, line
    assert snapshot(results_dir) == before


def test_CP1_T111_an_interrupt_while_clearing_markers_says_force_and_force_then_finishes(
    mixed_exports: Path, results_dir: Path, run_cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP1-T111] A1: Ctrl-C before every .done was cleared names --force (stderr and run.log); --force then gives v2."""
    _r15_finish_v1(mixed_exports, results_dir, run_cli)
    real_remove = _r15_safefs.remove

    def remove(root: Path, target: Path) -> bool:
        if target.name == a2m_layout.DONE_MARKER_NAME and target.parent.name == "beta":
            raise KeyboardInterrupt
        return real_remove(root, target)

    monkeypatch.setattr(_r15_safefs, "remove", remove)
    calls: list[str] = []
    res = run_cli(base_argv(mixed_exports, results_dir, "--force"), stages=[_r15_versioned("v2", calls)])

    assert res.code == 130, (res.out, res.err)
    line = _one_stderr_line(res.err)
    assert "rerun with --force" in line and "--resume" not in line, line
    assert calls == []
    entries = [e for e in log_lines(run_log(results_dir)) if "run interrupted" in e]
    assert len(entries) == 1 and "rerun with --force" in entries[0] and "--resume" not in entries[0], entries

    monkeypatch.setattr(_r15_safefs, "remove", real_remove)
    res = run_cli(base_argv(mixed_exports, results_dir, "--force"), stages=[_r15_versioned("v2", calls)])
    assert res.code == 0, res.err
    assert calls == ["alpha", "beta", "gamma"]
    assert _r15_outputs(results_dir) == dict.fromkeys(ALL3, "v2")


def test_CP1_T111_a_marker_force_cannot_remove_fails_that_proxy_only(
    mixed_exports: Path, results_dir: Path, run_cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP1-T111] A1: a .done --force cannot remove marks that proxy failed (exit 1, logged, not run); the rest are redone."""
    _r15_finish_v1(mixed_exports, results_dir, run_cli)
    real_remove = _r15_safefs.remove

    def remove(root: Path, target: Path) -> bool:
        if target.name == a2m_layout.DONE_MARKER_NAME and target.parent.name == "gamma":
            raise PermissionError(13, "Permission denied", str(target))
        return real_remove(root, target)

    monkeypatch.setattr(_r15_safefs, "remove", remove)
    calls: list[str] = []
    res = run_cli(base_argv(mixed_exports, results_dir, "--force"), stages=[_r15_versioned("v2", calls)])

    assert res.code == 1, (res.out, res.err)
    assert "2 done, 0 skipped as already done, 0 refused, 1 failed" in res.out, res.out
    assert calls == ["alpha", "beta"]
    assert _r15_outputs(results_dir) == {"alpha": "v2", "beta": "v2", "gamma": "v1"}
    failed = [e for e in log_lines(run_log(results_dir)) if "failed gamma" in e]
    assert len(failed) == 1 and "could not remove its earlier .done marker" in failed[0], failed


def test_CP1_T111_only_runs_get_advice_that_keeps_only(
    mixed_exports: Path, results_dir: Path, run_cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP1-T111] A1: --force --only beta interrupted names --only beta; --resume --only beta then finishes just beta."""
    _r15_finish_v1(mixed_exports, results_dir, run_cli)

    calls: list[str] = []
    res = run_cli(
        base_argv(mixed_exports, results_dir, "--force", "--only", "beta"), stages=[_r15_versioned("v2", calls, "beta")]
    )
    assert res.code == 130, (res.out, res.err)
    assert "rerun with --resume --only beta" in _one_stderr_line(res.err), res.err
    assert marker_names(results_dir) == {"alpha", "gamma"}

    calls.clear()
    res = run_cli(base_argv(mixed_exports, results_dir, "--resume", "--only", "beta"), stages=[_r15_versioned("v2", calls)])
    assert res.code == 0, res.err
    assert calls == ["beta"]
    assert _r15_outputs(results_dir) == {"alpha": "v1", "beta": "v2", "gamma": "v1"}
    assert marker_names(results_dir) == ALL3

    def interrupted(_input_dir: Path):
        raise KeyboardInterrupt

    monkeypatch.setattr(_r15_engine, "discover", interrupted)
    res = run_cli(base_argv(mixed_exports, results_dir, "--force", "--only", "beta"), stages=[_r15_versioned("v2", [])])
    assert res.code == 130, (res.out, res.err)
    assert "rerun with --force --only beta" in _one_stderr_line(res.err), res.err


# ---------------------------------------------------------------- CP1 adversarial round 15, A1 follow-up
# A --force run brackets the .done removal with a run-level force-pending marker; while it exists --resume
# is refused, so a kill in the middle of the removal can never let --resume keep stale output.


_R15_REAL_REMOVE = _r15_safefs.remove


def _r15_kill_when_removing(monkeypatch: pytest.MonkeyPatch, owner: str, name: str) -> None:
    real_remove = _R15_REAL_REMOVE

    def remove(root: Path, target: Path) -> bool:
        if target.name == name and (owner == "" or target.parent.name == owner):
            raise _R15Killed
        return real_remove(root, target)

    monkeypatch.setattr(_r15_safefs, "remove", remove)


@pytest.mark.parametrize(
    ("owner", "name"),
    [("beta", ".done"), ("", ".a2m-force-pending")],
    ids=["CP1-T112-kill-mid-removal", "CP1-T112-kill-before-pending-removed"],
)
def test_CP1_T112_a_force_run_killed_while_removing_markers_makes_resume_refuse(
    mixed_exports: Path, results_dir: Path, run_cli, monkeypatch: pytest.MonkeyPatch, owner: str, name: str
) -> None:
    """[CP1-T112] A kill during the .done removal leaves the pending marker; --resume refuses naming --force; --force finishes."""
    _r15_finish_v1(mixed_exports, results_dir, run_cli)
    _r15_kill_when_removing(monkeypatch, owner, name)
    calls: list[str] = []
    with pytest.raises(_R15Killed):
        run_cli(base_argv(mixed_exports, results_dir, "--force"), stages=[_r15_versioned("v2", calls)])
    monkeypatch.setattr(_r15_safefs, "remove", _R15_REAL_REMOVE)
    assert calls == []
    assert (results_dir / a2m_layout.FORCE_PENDING_NAME).is_file()
    if owner == "beta":
        assert marker_names(results_dir) == {"beta", "gamma"}, "the kill landed mid-removal"
    before = snapshot(results_dir)

    res = run_cli(base_argv(mixed_exports, results_dir, "--resume"), stages=[_r15_versioned("v2", calls)])
    assert res.code == 2, (res.out, res.err)
    line = _one_stderr_line(res.err)
    assert "usage error" in line and "rerun with --force" in line, line
    assert calls == []
    assert snapshot(results_dir) == before

    res = run_cli(base_argv(mixed_exports, results_dir, "--force"), stages=[_r15_versioned("v2", calls)])
    assert res.code == 0, res.err
    assert calls == ["alpha", "beta", "gamma"]
    assert _r15_outputs(results_dir) == dict.fromkeys(ALL3, "v2")
    assert not (results_dir / a2m_layout.FORCE_PENDING_NAME).exists()


def test_CP1_T112_a_later_force_only_run_removes_every_stale_marker_left_by_the_killed_one(
    mixed_exports: Path, results_dir: Path, run_cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[CP1-T112] After a killed --force, --force --only gamma also removes beta's old .done; --resume then redoes it."""
    _r15_finish_v1(mixed_exports, results_dir, run_cli)
    _r15_kill_when_removing(monkeypatch, "beta", ".done")
    with pytest.raises(_R15Killed):
        run_cli(base_argv(mixed_exports, results_dir, "--force"), stages=[_r15_versioned("v2", [])])
    monkeypatch.setattr(_r15_safefs, "remove", _R15_REAL_REMOVE)

    calls: list[str] = []
    res = run_cli(base_argv(mixed_exports, results_dir, "--force", "--only", "gamma"), stages=[_r15_versioned("v2", calls)])
    assert res.code == 0, res.err
    assert calls == ["gamma"]
    assert marker_names(results_dir) == {"gamma"}
    assert not (results_dir / a2m_layout.FORCE_PENDING_NAME).exists()

    calls.clear()
    res = run_cli(base_argv(mixed_exports, results_dir, "--resume"), stages=[_r15_versioned("v2", calls)])
    assert res.code == 0, res.err
    assert calls == ["alpha", "beta"]
    assert _r15_outputs(results_dir) == dict.fromkeys(ALL3, "v2")


def test_CP1_T112_a_finished_force_run_leaves_no_pending_marker_and_resume_trusts_it(
    mixed_exports: Path, results_dir: Path, run_cli
) -> None:
    """[CP1-T112] Guard: a --force run that finishes removes the pending marker; --resume then skips every proxy."""
    _r15_finish_v1(mixed_exports, results_dir, run_cli)
    calls: list[str] = []
    res = run_cli(base_argv(mixed_exports, results_dir, "--force"), stages=[_r15_versioned("v2", calls)])
    assert res.code == 0, res.err
    assert not (results_dir / a2m_layout.FORCE_PENDING_NAME).exists()

    calls.clear()
    res = run_cli(base_argv(mixed_exports, results_dir, "--resume"), stages=[_r15_versioned("v3", calls)])
    assert res.code == 0, res.err
    assert calls == []
    assert _r15_outputs(results_dir) == dict.fromkeys(ALL3, "v2")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_CP1_T112_a_link_at_the_pending_marker_is_refused_and_not_followed(
    mixed_exports: Path, results_dir: Path, run_cli, tmp_path: Path
) -> None:
    """[CP1-T112] A symlink at the pending marker is refused (exit 2) and its target is never written or removed."""
    _r15_finish_v1(mixed_exports, results_dir, run_cli)
    target = tmp_path / "outside.txt"
    target.write_text("mine\n", encoding="utf-8")
    (results_dir / a2m_layout.FORCE_PENDING_NAME).symlink_to(target)

    for flag in ("--force", "--resume"):
        res = run_cli(base_argv(mixed_exports, results_dir, flag), stages=[_r15_versioned("v2", [])])
        assert res.code == 2, (flag, res.out, res.err)
        assert "symbolic link" in _one_stderr_line(res.err), res.err
    assert target.read_text(encoding="utf-8") == "mine\n"
    assert _r15_outputs(results_dir) == dict.fromkeys(ALL3, "v1")
    assert a2m_layout.unsafe_name_reason(a2m_layout.FORCE_PENDING_NAME.upper()) is not None


# ---------------------------------------------------------------- CP1 adversarial round 16, findings A1 and A2
# A1: zip members whose Unix mode is a link, FIFO, socket or device are treated like the folder equivalents.
# A2: the --only value in rerun advice is quoted so the advice can be pasted back. New imports live here.

import shlex as _r16_shlex  # noqa: E402
import shutil as _r16_shutil  # noqa: E402

from conftest import bundle_files as _r16_bundle_files  # noqa: E402

from a2m.discovery import MemberKind as _R16MemberKind  # noqa: E402
from a2m.discovery import member_kind as _r16_member_kind  # noqa: E402

_R16_SCRIPT = "apiproxy/resources/jsc/common.js"
_R16_TARGET = "../../../common/common.js"


def _r16_zip(path: Path, odd: list[tuple[str, int | None, str]], *, bundle: bool = True) -> None:
    """The alpha bundle (unless ``bundle`` is False) plus members with an explicit Unix mode (None: DOS, attr 0)."""
    with zipfile.ZipFile(path, "w") as zf:
        if bundle:
            for rel, text in _r16_bundle_files("alpha").items():
                zf.writestr(zipfile.ZipInfo(rel, (2026, 1, 1, 0, 0, 0)), text)
        for name, mode, data in odd:
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            if mode is None:
                info.create_system = 0
                info.external_attr = 0
            else:
                info.create_system = 3
                info.external_attr = mode << 16
            zf.writestr(info, data)


def _r16_work_files(results: Path) -> list[Path]:
    work = results / ".a2m-work"
    return sorted(p for p in work.rglob("*") if not p.is_dir()) if work.exists() else []


def _r16_alpha_folder(exports: Path, kind: str) -> None:
    """alpha/ as a folder bundle with ``kind`` ('link' or 'fifo') at the shared-script path."""
    write_bundle_dir(exports, "alpha")
    path = exports / "alpha" / _R16_SCRIPT
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "link":
        path.symlink_to(_R16_TARGET)
    else:
        os.mkfifo(path)


@pytest.mark.parametrize(
    "form",
    ["zip", "folder"],
    ids=["CP1-T113-zip-link-member-in-bundle-root", "CP1-T113-folder-link-in-bundle-root-parity"],
)
def test_CP1_T113_a_link_inside_the_bundle_root_is_refused_for_a_zip_as_for_a_folder(
    tmp_path: Path, results_dir: Path, run_cli, form: str
) -> None:
    """[CP1-T113] A1 repro: a link at apiproxy/resources/jsc/common.js refuses alpha (zip and folder); gamma is done."""
    exports = tmp_path / "exports"
    exports.mkdir()
    if form == "zip":
        _r16_zip(exports / "alpha.zip", [(_R16_SCRIPT, stat.S_IFLNK | 0o777, _R16_TARGET)])
    else:
        _r16_alpha_folder(exports, "link")
    write_bundle_dir(exports, "gamma")
    seen: dict[str, bytes] = {}

    def stage(proxy) -> None:
        script = Path(proxy.bundle_dir) / _R16_SCRIPT
        if script.exists():
            seen[proxy.name] = script.read_bytes()
        Path(proxy.out_dir).mkdir(parents=True, exist_ok=True)

    res = run_cli(base_argv(exports, results_dir), stages=[stage])

    assert res.code == 0, (res.out, res.err)
    assert marker_names(results_dir) == {"gamma"}
    assert "alpha" not in seen, seen
    refused = [line for line in log_lines(run_log(results_dir)) if "refused alpha" in line]
    assert len(refused) == 1, run_log(results_dir)
    assert _R16_SCRIPT in refused[0] and "symbolic link" in refused[0], refused
    assert _r16_work_files(results_dir) == []


def test_CP1_T113_a_zip_that_gains_a_link_member_loses_its_stale_done_on_resume(
    tmp_path: Path, results_dir: Path, run_cli, recorder
) -> None:
    """[CP1-T113] A1: alpha.zip finished, then re-exported with a link member: --resume refuses it and removes its .done."""
    exports = tmp_path / "exports"
    exports.mkdir()
    _r16_zip(exports / "alpha.zip", [(_R16_SCRIPT, stat.S_IFREG | 0o644, "var shared = 1;\n")])
    write_bundle_dir(exports, "gamma")
    res = run_cli(base_argv(exports, results_dir), stages=[recorder])
    assert res.code == 0 and marker_names(results_dir) == {"alpha", "gamma"}, res.err

    _r16_zip(exports / "alpha.zip", [(_R16_SCRIPT, stat.S_IFLNK | 0o777, _R16_TARGET)])
    recorder.calls.clear()
    res = run_cli(base_argv(exports, results_dir, "--resume"), stages=[recorder])

    assert res.code == 0, (res.out, res.err)
    assert recorder.calls == []
    assert marker_names(results_dir) == {"gamma"}
    assert any("refused alpha" in line and "symbolic link" in line for line in log_lines(run_log(results_dir)))


@pytest.mark.skipif(_r16_shutil.which("zip") is None, reason="the zip tool is not installed")
def test_CP1_T113_a_bundle_zipped_with_zip_ry_and_a_symlinked_script_is_refused(
    tmp_path: Path, results_dir: Path, run_cli, recorder
) -> None:
    """[CP1-T113] A1 repro with the real tool: `zip -ry alpha.zip apiproxy` stores the link; a2m refuses alpha."""
    src = tmp_path / "src"
    write_bundle_dir(src, "alpha")
    (src / "common").mkdir()
    (src / "common" / "common.js").write_text("var shared = 1;\n", encoding="utf-8")
    script = src / "alpha" / _R16_SCRIPT
    script.parent.mkdir(parents=True)
    script.symlink_to(_R16_TARGET)
    exports = tmp_path / "exports"
    exports.mkdir()
    subprocess.run(
        ["zip", "-qry", str(exports / "alpha.zip"), "apiproxy"], cwd=src / "alpha", check=True, capture_output=True
    )
    write_bundle_dir(exports, "gamma")

    res = run_cli(base_argv(exports, results_dir), stages=[recorder])

    assert res.code == 0, (res.out, res.err)
    assert recorder.calls == ["gamma"]
    assert any(
        "refused alpha" in line and _R16_SCRIPT in line and "symbolic link" in line
        for line in log_lines(run_log(results_dir))
    ), run_log(results_dir)


@pytest.mark.parametrize(
    ("form", "mode"),
    [
        ("zip", stat.S_IFIFO | 0o644),
        ("zip", stat.S_IFCHR | 0o644),
        ("zip", stat.S_IFSOCK | 0o644),
        ("folder", None),
    ],
    ids=["CP1-T113-zip-fifo-member", "CP1-T113-zip-device-member", "CP1-T113-zip-socket-member", "CP1-T113-folder-fifo"],
)
def test_CP1_T113_a_special_file_inside_the_bundle_root_is_refused(
    tmp_path: Path, results_dir: Path, run_cli, recorder, form: str, mode: int | None
) -> None:
    """[CP1-T113] A1: a FIFO, device or socket member in the bundle root refuses alpha, as a FIFO in a folder does."""
    exports = tmp_path / "exports"
    exports.mkdir()
    if form == "zip":
        assert mode is not None
        _r16_zip(exports / "alpha.zip", [(_R16_SCRIPT, mode, "")])
    else:
        _r16_alpha_folder(exports, "fifo")
    write_bundle_dir(exports, "gamma")

    res = run_cli(base_argv(exports, results_dir), stages=[recorder])

    assert res.code == 0, (res.out, res.err)
    assert recorder.calls == ["gamma"]
    assert any(
        "refused alpha" in line and _R16_SCRIPT in line and "special files" in line
        for line in log_lines(run_log(results_dir))
    ), run_log(results_dir)
    assert _r16_work_files(results_dir) == []


@pytest.mark.parametrize("form", ["zip", "folder"], ids=["CP1-T113-zip-apiproxy-link", "CP1-T113-folder-apiproxy-link"])
def test_CP1_T113_an_item_whose_only_apiproxy_is_a_link_is_refused_not_skipped(
    tmp_path: Path, results_dir: Path, run_cli, recorder, form: str
) -> None:
    """[CP1-T113] A1: a zip whose only apiproxy entry is a link member is refused, as a folder with a link apiproxy is."""
    exports = tmp_path / "exports"
    exports.mkdir()
    if form == "zip":
        _r16_zip(exports / "alpha.zip", [("apiproxy", stat.S_IFLNK | 0o777, "../real/apiproxy")], bundle=False)
    else:
        (exports / "alpha").mkdir()
        (exports / "alpha" / "apiproxy").symlink_to(tmp_path / "elsewhere")
    write_bundle_dir(exports, "gamma")

    res = run_cli(base_argv(exports, results_dir), stages=[recorder])

    assert res.code == 0, (res.out, res.err)
    assert recorder.calls == ["gamma"]
    lines = log_lines(run_log(results_dir))
    assert any("refused alpha" in line and "symbolic link" in line for line in lines), lines
    assert not any("skipped alpha" in line for line in lines), lines


_R16_LINK = stat.S_IFLNK | 0o777
_R16_LAYOUTS: dict[str, list[tuple[str, int | None, str]]] = {
    "top-apiproxy-link": [("apiproxy", _R16_LINK, "x")],
    "wrapped-apiproxy-link": [("alpha/apiproxy", _R16_LINK, "x")],
    "shared-flow-link": [("sharedflowbundle", _R16_LINK, "x")],
    "deep-apiproxy-link": [("a/b/apiproxy", _R16_LINK, "x"), ("a/b/readme.txt", None, "hi")],
    "real-root-beside-a-link-root": [("apiproxy/alpha.xml", None, "<APIProxy/>\n"), ("docs/apiproxy", _R16_LINK, "x")],
    "link-file-in-docs": [("apiproxy/alpha.xml", None, "<APIProxy/>\n"), ("docs/readme", _R16_LINK, "x")],
}


@pytest.mark.parametrize("layout", sorted(_R16_LAYOUTS), ids=[f"CP1-T113-parity-{k}" for k in sorted(_R16_LAYOUTS)])
def test_CP1_T113_bundle_root_decides_the_same_for_links_in_a_folder_and_a_zip(tmp_path: Path, layout: str) -> None:
    """[CP1-T113] A1: CP1-T85's folder/zip parity holds when links are involved (link members vs real symlinks)."""
    members = _R16_LAYOUTS[layout]
    folder = tmp_path / "item"
    folder.mkdir()
    for name, mode, data in members:
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode is None:
            path.write_text(data, encoding="utf-8")
        else:
            path.symlink_to(tmp_path / "elsewhere")
    zip_path = tmp_path / "item.zip"
    _r16_zip(zip_path, members, bundle=False)
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()

    answers = []
    for subfolders in (folder_subfolders(folder), zip_subfolders(infos)):
        try:
            answers.append(bundle_root(subfolders))
        except BundleLayoutError as exc:
            answers.append(str(exc))
    assert answers[0] == answers[1], answers
    assert zip_folder_tree(infos)[()] == zip_top_folders(infos)


@pytest.mark.parametrize(
    "where",
    ["docs/shared.js", "notes"],
    ids=["CP1-T113-link-member-in-a-folder-beside-the-root", "CP1-T113-link-member-at-the-top-beside-the-root"],
)
@pytest.mark.parametrize("form", ["zip", "folder"], ids=["zip", "folder"])
def test_CP1_T113_a_link_beside_the_bundle_root_is_left_out_not_refused(
    tmp_path: Path, results_dir: Path, run_cli, form: str, where: str
) -> None:
    """[CP1-T113] A1 guard: a link outside the bundle root is left out (never unpacked), for a zip as for a folder."""
    exports = tmp_path / "exports"
    exports.mkdir()
    if form == "zip":
        _r16_zip(exports / "alpha.zip", [(where, _R16_LINK, "/etc/passwd")])
    else:
        write_bundle_dir(exports, "alpha")
        link = exports / "alpha" / where
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to("/etc/passwd")
    seen: dict[str, bool] = {}

    def stage(proxy) -> None:
        seen[proxy.name] = (Path(proxy.bundle_dir) / where).exists() or (Path(proxy.bundle_dir) / where).is_symlink()
        Path(proxy.out_dir).mkdir(parents=True, exist_ok=True)

    res = run_cli(base_argv(exports, results_dir), stages=[stage])

    assert res.code == 0, (res.out, res.err)
    assert marker_names(results_dir) == {"alpha"}
    assert seen == {"alpha": False}, seen


@pytest.mark.parametrize(
    "mode",
    [None, stat.S_IFREG | 0o644, 0o644],
    ids=["CP1-T113-guard-dos-attr-0", "CP1-T113-guard-unix-regular", "CP1-T113-guard-unix-type-0"],
)
def test_CP1_T113_plain_members_from_dos_or_unix_zips_are_still_unpacked(
    tmp_path: Path, results_dir: Path, run_cli, mode: int | None
) -> None:
    """[CP1-T113] A1 guard: external_attr 0 (DOS) and 0o100644 members are accepted and unpacked with their bytes."""
    exports = tmp_path / "exports"
    exports.mkdir()
    _r16_zip(exports / "alpha.zip", [(_R16_SCRIPT, mode, "var shared = 1;\n"), ("apiproxy/policies/", None, "")])
    seen: dict[str, bytes] = {}

    def stage(proxy) -> None:
        seen[proxy.name] = (Path(proxy.bundle_dir) / _R16_SCRIPT).read_bytes()
        Path(proxy.out_dir).mkdir(parents=True, exist_ok=True)

    res = run_cli(base_argv(exports, results_dir), stages=[stage])

    assert res.code == 0, (res.out, res.err)
    assert seen == {"alpha": b"var shared = 1;\n"}
    assert marker_names(results_dir) == {"alpha"}


def test_CP1_T113_member_kind_reads_the_unix_type_bits() -> None:
    """[CP1-T113] A1: the one member-kind rule: link, special, folder and file, DOS members by name."""

    def info(name: str, system: int, mode: int) -> zipfile.ZipInfo:
        member = zipfile.ZipInfo(name)
        member.create_system = system
        member.external_attr = mode << 16
        return member

    assert _r16_member_kind(info("a", 3, stat.S_IFLNK | 0o777)) is _R16MemberKind.LINK
    assert _r16_member_kind(info("a", 3, stat.S_IFIFO | 0o644)) is _R16MemberKind.SPECIAL
    assert _r16_member_kind(info("a", 3, stat.S_IFBLK | 0o644)) is _R16MemberKind.SPECIAL
    assert _r16_member_kind(info("a/", 3, stat.S_IFDIR | 0o755)) is _R16MemberKind.FOLDER
    assert _r16_member_kind(info("a", 3, stat.S_IFREG | 0o644)) is _R16MemberKind.FILE
    assert _r16_member_kind(info("a", 0, 0)) is _R16MemberKind.FILE
    assert _r16_member_kind(info("a/", 0, 0)) is _R16MemberKind.FOLDER
    # The link bits mean nothing in a member made on DOS (create_system 0): its name decides.
    assert _r16_member_kind(info("a", 0, stat.S_IFLNK | 0o777)) is _R16MemberKind.FILE


def _r16_advice_flags(err: str) -> list[str]:
    line = _one_stderr_line(err)
    match = re.search(r"rerun with (.*) to (continue|redo every proxy)", line)
    assert match is not None, line
    return _r16_shlex.split(match.group(1))


@pytest.mark.parametrize("name", ["Order API", "-v2", "it's"], ids=["CP1-T114-space", "CP1-T114-dash", "CP1-T114-quote"])
def test_CP1_T114_pasted_rerun_advice_finishes_an_only_proxy_whose_name_needs_quoting(
    tmp_path: Path, results_dir: Path, run_cli, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """[CP1-T114] A2: --force --only NAME interrupted; the advice, split by shlex and passed back, finishes NAME."""
    exports = tmp_path / "exports"
    exports.mkdir()
    write_bundle_dir(exports, name)
    write_bundle_dir(exports, "gamma")

    calls: list[str] = []
    res = run_cli(base_argv(exports, results_dir, "--force", f"--only={name}"), stages=[_r15_versioned("v2", calls, name)])
    assert res.code == 130, (res.out, res.err)
    flags = _r16_advice_flags(res.err)
    assert "--resume" in flags, flags
    assert any(
        "run interrupted" in line and f"rerun with {_r16_shlex.join(flags)}" in line
        for line in log_lines(run_log(results_dir))
    ), run_log(results_dir)

    calls.clear()
    res = run_cli(base_argv(exports, results_dir, *flags), stages=[_r15_versioned("v2", calls)])
    assert res.code == 0, (res.out, res.err)
    assert calls == [name]
    assert marker_names(results_dir) == {name}

    def interrupted(_input_dir: Path):
        raise KeyboardInterrupt

    monkeypatch.setattr(_r15_engine, "discover", interrupted)
    res = run_cli(base_argv(exports, results_dir, "--force", f"--only={name}"), stages=[_r15_versioned("v2", [])])
    assert res.code == 130, (res.out, res.err)
    flags = _r16_advice_flags(res.err)
    assert "--force" in flags, flags
    monkeypatch.undo()
    calls.clear()
    res = run_cli(base_argv(exports, results_dir, *flags), stages=[_r15_versioned("v3", calls)])
    assert res.code == 0, (res.out, res.err)
    assert calls == [name]
