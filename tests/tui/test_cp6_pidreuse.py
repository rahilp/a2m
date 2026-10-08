"""TUI CP6 adversarial round 5: a2m never signals an unrelated process that took a recorded Mule PID.

E1: every SIGTERM and SIGKILL a2m sends to a recorded Mule PID (the leftover path of a runner that never
started, and the started runner's own stop) goes only to the process that was checked to be this results
folder's runtime. A process that later took one of those PIDs is treated as gone and is never signalled.

PID reuse is forced deterministically inside a private user + PID namespace (``unshare -Urpf --mount-proc``),
where ``/proc/sys/kernel/ns_last_pid`` can be written. Every process in a scenario is started by the scenario
script inside that namespace, so ending the namespace (``--kill-child``, bounded by a timeout) ends only
processes the test started; nothing outside it is ever signalled.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCENARIO_SECONDS = 90.0


def _unshare_works() -> bool:
    if shutil.which("unshare") is None or not Path("/proc/self/stat").exists():
        return False
    try:
        done = subprocess.run(
            ["unshare", "-Urpf", "--mount-proc", "--kill-child", "true"],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=20.0, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


needs_pid_namespace = pytest.mark.skipif(
    not _unshare_works(), reason="needs unprivileged user + PID namespaces (unshare -Urpf) to force PID reuse"
)

COMMON = r'''
import json, os, signal, subprocess, sys, threading, time
from pathlib import Path

import a2m.verify.mule as mule

if os.environ.get("A2M_TEST_NO_PIDFD") == "1":
    mule._pidfd_open = None  # the start-time fallback, for systems without pidfds
if os.environ.get("A2M_TEST_PS") == "1":
    mule._processes = mule._PsTable()  # process facts from ps, as on macOS (no /proc, no pidfds)
    mule._pidfd_open = None
if os.environ.get("A2M_TEST_NO_WAITID") == "1":
    mule._waitid = None  # seeing the JVM exit reaps it, as on macOS before Python 3.13

plain_env = {k: v for k, v in os.environ.items() if k != "MULE_BASE"}


def take_pid(pid, tries=50, session=False):
    """Start an unrelated process (no -Dmule.base) on exactly ``pid`` (free by now), or None if it never lands there.

    Each try is checked: a process that landed on another PID is ended and the handoff is tried again (bounded),
    so the scenario only goes on once the reuse it exists to prove has really happened. With ``session`` it
    leads its own process group, so the PID is a group ID too."""
    for _ in range(tries):
        Path("/proc/sys/kernel/ns_last_pid").write_text(str(pid - 1))
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], env=plain_env, start_new_session=session
        )
        if proc.pid == pid:
            return proc
        proc.kill()
        proc.wait()
        time.sleep(0.05)
    return None


def reap(pid, seconds=30.0):
    """Wait until ``pid`` has ended and is reaped (as the namespace's init, orphans end up our children)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            if not Path(f"/proc/{pid}").exists():
                return True
            done = 0
        if done == pid:
            return True
        time.sleep(0.01)
    return False
'''

LEFTOVER_SCENARIO = COMMON + r'''
mule.LEFTOVER_STOP_SECONDS = 3.0
tmp = Path(sys.argv[1])
base = (tmp / "base").absolute()
base.mkdir()
marker = f"-Dmule.base={base}"
# Leftover A ends at once on SIGTERM; leftover B takes 2 s to end after SIGTERM (like a JVM shutting down). Both
# name the base on their command line, as the JVM a2m starts does.
a = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", marker])
b = subprocess.Popen([sys.executable, "-c",
    "import signal,time,sys\nsignal.signal(signal.SIGTERM, lambda *_: (time.sleep(2), sys.exit(0)))\n"
    "print('ready', flush=True)\ntime.sleep(60)", marker], stdout=subprocess.PIPE)
b.stdout.readline()
(base / mule.PID_FILE).write_text(json.dumps({"pgid": a.pid, "pids": [a.pid, b.pid]}))
stranger = []


def reuse():
    a.wait()  # A ended by the SIGTERM; an unrelated process takes its PID while B still stops
    stranger.append(take_pid(a.pid))


t = threading.Thread(target=reuse)
t.start()
mule.MuleRunner(mule_home=tmp / "home", mule_base=base).stop()
t.join(30)
b.wait(30)
u = stranger[0] if stranger else None
time.sleep(0.3)
print(json.dumps({"same_pid": u is not None and u.pid == a.pid, "stranger": u.poll() if u else "none",
                  "a": a.returncode, "b": b.returncode, "pid_file_left": (base / mule.PID_FILE).exists()}))
if u is not None:
    u.kill()
    u.wait()
'''

STARTED_SETUP = COMMON + r'''
tmp = Path(sys.argv[1])
home = tmp / "home"
(home / "services").mkdir(parents=True)
(home / "conf").mkdir()
java_home = tmp / "java-home"
(java_home / "bin").mkdir(parents=True)
fifo = tmp / "block"
os.mkfifo(fifo)
# The "JVM" a2m starts: it first starts a helper in its process group that ends on SIGTERM (its PID written to
# helper.pid), then ignores SIGTERM itself and blocks in bash's builtin read on a FIFO nobody writes, so it never
# forks again and no child of it can take a PID the scenario is about to hand to an unrelated process.
java = java_home / "bin" / "java"
java.write_text(
    "#!" + sys.argv[3] + "\n"
    f'"{sys.argv[2]}" 60 &\n'
    'echo $! > helper.pid\n'
    'trap "" TERM\n'
    "echo 'Mule is up and kicking'\n"
    f'exec 3<>"{fifo}"; while :; do read -r -u 3 _; done\n'
)
java.chmod(0o755)
os.environ["JAVA_HOME"] = str(java_home)
base = (tmp / "base").absolute()
runner = mule.MuleRunner(mule_home=home, mule_base=base)
runner.start(timeout=20)
jvm = runner.pids[0]
helper = int((base / "helper.pid").read_text())
'''

STARTED_SCENARIO = STARTED_SETUP + r'''
stranger = []


def reuse():
    # The helper ends on the SIGTERM to the JVM's group and is reaped; an unrelated process then takes its PID.
    if reap(helper):
        stranger.append(take_pid(helper))


t = threading.Thread(target=reuse)
t.start()
runner.stop(timeout=3)
t.join(30)
u = stranger[0] if stranger else None
time.sleep(0.3)
print(json.dumps({"same_pid": u is not None and u.pid == helper, "stranger": u.poll() if u else "none",
                  "jvm_gone": not Path(f"/proc/{jvm}").exists()}))
if u is not None:
    u.kill()
    u.wait()
'''


JVM_REUSE_SCENARIO = STARTED_SETUP + r'''
before = runner.health_problem()
os.kill(jvm, signal.SIGKILL)  # the JVM this scenario's runner started dies, and so does its helper
os.kill(helper, signal.SIGKILL)
reap(helper)  # orphaned by the JVM, the helper is the namespace init's (this script's) to reap
deadline = time.monotonic() + 10
while runner.health_problem() is None and time.monotonic() < deadline:
    time.sleep(0.05)
problem = runner.health_problem()
# Nothing else is left in the JVM's group: only a2m not reaping its JVM keeps the PID (and group ID) taken.
early = take_pid(jvm, tries=20, session=True)
runner.stop(timeout=3)
# Reaped by stop, the PID is free: an unrelated process leading its own group (its group ID is the old JVM's)
# takes it, and nothing the runner does afterwards signals it.
u = take_pid(jvm, session=True)
after = runner.health_problem()
runner.stop(timeout=3)
time.sleep(0.3)
print(json.dumps({"before": before, "problem": problem, "taken_before_stop": early is not None,
                  "taken_after_stop": u is not None, "after": after, "stranger": u.poll() if u else "none"}))
for proc in (early, u):
    if proc is not None:
        proc.kill()
        proc.wait()
'''


MODES = {
    "pidfd": {},
    "start-time": {"A2M_TEST_NO_PIDFD": "1"},
    "ps": {"A2M_TEST_PS": "1"},
    "waitid": {},
    "zombie-proc": {"A2M_TEST_NO_WAITID": "1"},
    "zombie-ps": {"A2M_TEST_NO_WAITID": "1", "A2M_TEST_PS": "1"},
}


def _run_scenario(tmp_path: Path, script: str, *, mode: str, args: list[str] | None = None) -> dict[str, object]:
    path = tmp_path / "scenario.py"
    path.write_text(script, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("MULE_BASE", "MULE_HOME", "FORCE_COLOR", "PY_COLORS")}
    env["PYTHONPATH"] = str(REPO)
    env.update(MODES[mode])
    done = subprocess.run(
        ["unshare", "-Urpf", "--mount-proc", "--kill-child", sys.executable, str(path), str(work), *(args or [])],
        cwd=REPO, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=SCENARIO_SECONDS, check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


@needs_pid_namespace
@pytest.mark.parametrize("mode", ["pidfd", "start-time", "ps"])
def test_TUI_CP6_X20_leftover_stop_never_signals_a_process_that_took_a_recorded_pid(tmp_path: Path, mode: str) -> None:
    """[TUI-CP6-X20] A never-started runner ends the leftovers recorded for its base (recognised by the
    -Dmule.base=<base> on their command line). When one of them ends on SIGTERM and an unrelated process (no
    -Dmule.base) takes its PID while another leftover still stops, that process is left alone: never SIGKILLed or
    signalled at all, with pidfds, with the start-time fallback, and with process facts from ps (as on macOS)."""
    result = _run_scenario(tmp_path, LEFTOVER_SCENARIO, mode=mode)
    assert result["same_pid"] is True, result
    assert result["stranger"] is None, result
    assert result["a"] == -15, result
    assert result["b"] == 0, result
    assert result["pid_file_left"] is False, result


def _sleep_and_bash() -> list[str]:
    sleep, bash = shutil.which("sleep"), shutil.which("bash")
    if sleep is None or bash is None:
        pytest.skip("needs sleep and bash")
    return [str(Path(sleep).resolve()), str(Path(bash).resolve())]


@needs_pid_namespace
def test_TUI_CP6_X21_started_runner_stop_never_kills_a_process_that_took_a_helper_pid(tmp_path: Path) -> None:
    """[TUI-CP6-X21] A started runner's stop: a helper in the JVM's process group ends on the SIGTERM and an
    unrelated process takes its PID while the JVM ignores SIGTERM. When stop falls back to SIGKILL (to the JVM's
    group only), the JVM is killed but the process that took the helper's PID survives untouched."""
    result = _run_scenario(tmp_path, STARTED_SCENARIO, mode="pidfd", args=_sleep_and_bash())
    assert result["same_pid"] is True, result
    assert result["stranger"] is None, result
    assert result["jvm_gone"] is True, result


@needs_pid_namespace
@pytest.mark.parametrize("mode", ["waitid", "zombie-proc", "zombie-ps"])
def test_TUI_CP6_X22_a_dead_jvm_is_seen_without_reaping_it_and_its_pid_is_never_signalled_after(
    tmp_path: Path, mode: str
) -> None:
    """[TUI-CP6-X22] A started runner's health check: when the JVM dies the runtime is reported as stopped, and
    the dead JVM is not reaped before stop (seen through waitid, or as a zombie in /proc or ps where waitid is
    missing, as on macOS before Python 3.13), so no other process can take its PID or group ID meanwhile. Once
    stop reaped it, a process that took the PID (leading its own group) is never signalled."""
    result = _run_scenario(tmp_path, JVM_REUSE_SCENARIO, mode=mode, args=_sleep_and_bash())
    assert result["before"] is None, result
    how = "was killed by signal 9" if mode == "waitid" else "exited"
    assert result["problem"] == f"the Mule runtime's JVM stopped (it {how})", result
    assert result["taken_before_stop"] is False, result
    assert result["taken_after_stop"] is True, result
    assert result["after"] == "the Mule runtime is not running", result
    assert result["stranger"] is None, result
