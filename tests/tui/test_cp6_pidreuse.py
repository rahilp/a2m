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

plain_env = {k: v for k, v in os.environ.items() if k != "MULE_BASE"}


def take_pid(pid, tries=50):
    """Start an unrelated process (no MULE_BASE) on exactly ``pid`` (free by now), or None if it never lands there.

    Each try is checked: a process that landed on another PID is ended and the handoff is tried again (bounded),
    so the scenario only goes on once the reuse it exists to prove has really happened."""
    for _ in range(tries):
        Path("/proc/sys/kernel/ns_last_pid").write_text(str(pid - 1))
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], env=plain_env)
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
env = dict(os.environ, MULE_BASE=str(base))
# Leftover A ends at once on SIGTERM; leftover B takes 2 s to end after SIGTERM (like a JVM shutting down).
a = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], env=env)
b = subprocess.Popen([sys.executable, "-c",
    "import signal,time,sys\nsignal.signal(signal.SIGTERM, lambda *_: (time.sleep(2), sys.exit(0)))\n"
    "print('ready', flush=True)\ntime.sleep(60)"], env=env, stdout=subprocess.PIPE)
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
import shutil

tmp = Path(sys.argv[1])
home = tmp / "home"
(home / "bin").mkdir(parents=True)
(home / "services").mkdir()
(home / "conf").mkdir()
fake = tmp / "fake"
fake.mkdir()
# A "wrapper" that ends on SIGTERM and a "JVM" that ignores it, as the launcher's children. The JVM is one
# process that never forks (it blocks in bash's builtin read on a FIFO nobody writes), so no child of it can
# take a PID the scenario is about to hand to an unrelated process.
shutil.copy(sys.argv[2], fake / "wrapper")
shutil.copy(sys.argv[3], fake / "java")
os.mkfifo(fake / "block")
launcher = home / "bin" / "mule"
launcher.write_text(
    "#!" + sys.argv[3] + "\n"
    'cd "$MULE_BASE"\n'
    f'"{fake}/wrapper" 60 &\n'
    f'"{fake}/java" -c \'trap "" TERM; exec 3<>"{fake}/block"; while :; do read -r -u 3 _; done\' &\n'
    "sleep 0.5\n"
    "echo 'Mule is up and kicking' >> logs/mule.log\n"
    "wait\n"
)
launcher.chmod(0o755)
base = (tmp / "base").absolute()
runner = mule.MuleRunner(mule_home=home, mule_base=base)
runner.start(timeout=20)
names = {pid: mule._executable_name(pid) for pid in runner.pids}
wrapper = next(pid for pid, name in names.items() if name == "wrapper")
jvm = next(pid for pid, name in names.items() if name == "java")
'''

STARTED_SCENARIO = STARTED_SETUP + r'''
stranger = []


def reuse():
    # The wrapper ends on the SIGTERM (its launcher too, so it is reparented to us); its PID is then taken.
    if reap(wrapper):
        stranger.append(take_pid(wrapper))


t = threading.Thread(target=reuse)
t.start()
runner.stop(timeout=3)
t.join(30)
u = stranger[0] if stranger else None
time.sleep(0.3)
jvm_gone = reap(jvm, 10)
print(json.dumps({"same_pid": u is not None and u.pid == wrapper, "stranger": u.poll() if u else "none",
                  "jvm_gone": jvm_gone}))
if u is not None:
    u.kill()
    u.wait()
'''


JVM_REUSE_SCENARIO = STARTED_SETUP + r'''
before = runner.health_problem()
os.kill(jvm, signal.SIGKILL)  # the JVM this scenario started dies; the launcher (still waiting) reaps it
u = take_pid(jvm) if reap(jvm, 10) else None
problem = runner.health_problem()
runner.stop(timeout=3)
time.sleep(0.3)
print(json.dumps({"before": before, "same_pid": u is not None and u.pid == jvm, "problem": problem,
                  "stranger": u.poll() if u else "none"}))
if u is not None:
    u.kill()
    u.wait()
'''


def _run_scenario(tmp_path: Path, script: str, *, no_pidfd: bool, args: list[str] | None = None) -> dict[str, object]:
    path = tmp_path / "scenario.py"
    path.write_text(script, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("MULE_BASE", "MULE_HOME", "FORCE_COLOR", "PY_COLORS")}
    env["PYTHONPATH"] = str(REPO)
    if no_pidfd:
        env["A2M_TEST_NO_PIDFD"] = "1"
    done = subprocess.run(
        ["unshare", "-Urpf", "--mount-proc", "--kill-child", sys.executable, str(path), str(work), *(args or [])],
        cwd=REPO, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=SCENARIO_SECONDS, check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


@needs_pid_namespace
@pytest.mark.parametrize("no_pidfd", [False, True], ids=["pidfd", "start-time"])
def test_TUI_CP6_X20_leftover_stop_never_signals_a_process_that_took_a_recorded_pid(
    tmp_path: Path, no_pidfd: bool
) -> None:
    """[TUI-CP6-X20] A never-started runner ends the leftovers recorded for its base. When one of them ends on
    SIGTERM and an unrelated process (no MULE_BASE) takes its PID while another leftover still stops, that
    process is left alone: never SIGKILLed or signalled at all, with pidfds and with the start-time fallback."""
    result = _run_scenario(tmp_path, LEFTOVER_SCENARIO, no_pidfd=no_pidfd)
    assert result["same_pid"] is True, result
    assert result["stranger"] is None, result
    assert result["a"] == -15, result
    assert result["b"] == 0, result
    assert result["pid_file_left"] is False, result


@needs_pid_namespace
@pytest.mark.parametrize("no_pidfd", [False, True], ids=["pidfd", "start-time"])
def test_TUI_CP6_X21_started_runner_stop_never_kills_a_process_that_took_the_wrapper_pid(
    tmp_path: Path, no_pidfd: bool
) -> None:
    """[TUI-CP6-X21] A started runner's stop: the recorded wrapper ends on SIGTERM and an unrelated process takes
    its PID while the JVM ignores SIGTERM. When stop falls back to SIGKILL, the JVM is killed but the process
    that took the wrapper's PID survives untouched."""
    sleep, bash = shutil.which("sleep"), shutil.which("bash")
    if sleep is None or bash is None:
        pytest.skip("needs sleep and bash")
    result = _run_scenario(
        tmp_path, STARTED_SCENARIO, no_pidfd=no_pidfd,
        args=[str(Path(sleep).resolve()), str(Path(bash).resolve())],
    )
    assert result["same_pid"] is True, result
    assert result["stranger"] is None, result
    assert result["jvm_gone"] is True, result


@needs_pid_namespace
@pytest.mark.parametrize("no_pidfd", [False, True], ids=["pidfd", "start-time"])
def test_TUI_CP6_X22_health_check_sees_a_dead_jvm_whose_pid_an_unrelated_process_took(
    tmp_path: Path, no_pidfd: bool
) -> None:
    """[TUI-CP6-X22] A started runner's health check: when the JVM dies and an unrelated process takes its PID,
    the runtime is reported as stopped (the new process is not mistaken for the JVM), and stop leaves it alone."""
    sleep, bash = shutil.which("sleep"), shutil.which("bash")
    if sleep is None or bash is None:
        pytest.skip("needs sleep and bash")
    result = _run_scenario(
        tmp_path, JVM_REUSE_SCENARIO, no_pidfd=no_pidfd,
        args=[str(Path(sleep).resolve()), str(Path(bash).resolve())],
    )
    assert result["before"] is None, result
    assert result["same_pid"] is True, result
    assert result["problem"] == "the Mule runtime's JVM stopped", result
    assert result["stranger"] is None, result
