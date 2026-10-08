"""A minimal runner for the local Mule standalone runtime (Mule Kernel CE).

:func:`package` builds a generated project with Maven. :class:`MuleRunner`
starts Mule headless under a private MULE_BASE (so the user's install under
MULE_HOME is only read, never written), hot-deploys built apps into
``<mule_base>/apps``, waits for each to start or fail, undeploys them and
stops Mule again.

Mule's JVM is started directly (:func:`jvm_command`), on every OS. Mule's own
launcher, ``bin/mule``, runs the JVM under the Tanuki wrapper, which Mule 4.9.0
ships only as a 32-bit macOS binary and not at all for ARM Linux; the JVM
settings are the ones its ``conf/wrapper.conf`` gives, with Mule's basic
container wrapper in place of Tanuki's. Without the Tanuki wrapper nothing
writes ``logs/mule.log``: the JVM's own output (stdout and stderr) is appended
to it instead, so the startup banner and every deploy line land there.

Deploy signals (proven on Mule 4.9.0): ``apps/<name>-anchor.txt`` appears and
``Started app '<name>'`` is logged when an app started; ``Failed to deploy
artifact`` in ``logs/mule.log`` means it did not. Both start signals can come
slightly before the app's HTTP listener serves: until then Mule's HTTP service
answers every request with a container-level 503 ("Server not available to
handle this request, ..."). So :meth:`MuleRunner.deploy` also probes each
listener port of the app (``http.listener.port`` in the jar's properties) until
Mule answers with anything else.

Java and Maven are found on PATH. The JVM run is ``$JAVA_HOME/bin/java``; when
JAVA_HOME is not set it is derived from the ``java`` on PATH (:func:`java_home`).

A started runtime is always stopped: the JVM is a2m's own child, leading its
own process group, and :meth:`MuleRunner.stop` ends that group (SIGTERM, then
SIGKILL after the timeout). The JVM is not reaped before that (its exit is seen
through ``waitid`` without reaping, or as a zombie in the process table), so its
PID and group ID cannot be taken by another process while a2m may still signal
them. An ``atexit`` hook
stops any runtime still running when Python exits, and the JVM's PID is written
to ``a2m-mule.pids`` in the MULE_BASE so a later start under the same base
first ends a JVM that an a2m killed outright left behind: only a recorded PID
whose command line names this MULE_BASE (``-Dmule.base=<base>``) is
signalled, never anything found by name. Each such process is pinned by
identity when it is chosen (a pidfd where the OS has them, else its start
time, checked again with its command line before each signal), so a process
that later took a recorded PID is never signalled. Process facts come from
``/proc`` on Linux and from ``ps`` elsewhere (macOS).
:meth:`MuleRunner.health_problem` says when a started runtime is no longer
usable (its JVM exited, which includes running out of memory: the JVM is told
to exit then), so a caller can restart it instead of blaming the next app.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from a2m import safefs
from a2m.generator.project import LISTENER_HOST_KEY, LISTENER_PORT_KEY

MULE_LOG = ("logs", "mule.log")
STARTED_SIGNAL = "Mule is up and kicking"
DEPLOY_FAILED_SIGNAL = "Failed to deploy artifact"
APP_STARTED_SIGNAL = "Started app '{name}'"
# The body of the 503 Mule's HTTP service sends while no server is ready on the port; the app's
# own responses (a 503 fault included) never carry it.
CONTAINER_UNAVAILABLE = b"Server not available to handle this request, either not initialized yet or it has been disposed."
# Mule answers "Expect: 100-continue" with "100 Continue" once a server serves the port, without
# running a flow (the body is never sent); before that it sends the container 503.
PROBE_PATH = "/a2m-readiness-probe"
PROBE_SECONDS = 2.0
PROBE_MAX_BYTES = 65536
CONTENT_LENGTH = re.compile(rb"\s*content-length\s*:\s*(\d+)\s*", re.IGNORECASE)
ANY_HOSTS = ("", "0.0.0.0", "::", "[::]")
JAR_SUFFIX = "-mule-application.jar"
POLL_SECONDS = 0.5
DEPLOY_POLL_SECONDS = 0.25
EXCERPT_LINES = 60
# The PIDs (and process group) of the runtime started under a MULE_BASE, kept there while it runs.
PID_FILE = "a2m-mule.pids"
LEFTOVER_STOP_SECONDS = 30.0
EXIT_STOP_SECONDS = 30.0
# mule.log only grows (each start appends); one that grew past this is moved to mule.log.1 before a start.
MULE_LOG_KEEP_BYTES = 16 * 1024 * 1024
# The JVM settings of Mule 4.9.0's conf/wrapper.conf and conf/java11-plus/wrapper.jvmDependant.conf, minus the
# Tanuki wrapper's own module and native library. -XX:+HeapDumpOnOutOfMemoryError and -XX:+AlwaysPreTouch are
# left out (a 1 GB heap dump in the results folder; memory committed up front); -XX:+ExitOnOutOfMemoryError
# stands in for the wrapper's restart of a JVM that ran out of memory, so health_problem sees it.
JVM_HEAP = ("-Xms1024m", "-Xmx1024m")
JVM_OPTIONS = (
    "-Djava.net.preferIPv4Stack=TRUE",
    "-Dorg.glassfish.grizzly.nio.transport.TCPNIOTransport.max-receive-buffer-size=1048576",
    "-Dorg.glassfish.grizzly.nio.transport.TCPNIOTransport.max-send-buffer-size=1048576",
    "-XX:MaxMetaspaceSize=256m",
    "-XX:MetaspaceSize=128m",
    "-XX:NewRatio=1",
    "-XX:MaxTenuringThreshold=8",
    "-XX:+ExitOnOutOfMemoryError",
    "-Dorg.quartz.scheduler.skipUpdateCheck=true",
    "-Dmule.metadata.cache.entryTtl.minutes=10",
    "-Dmule.metadata.cache.expirationInterval.millis=5000",
    "-Djava.locale.providers=COMPAT,CLDR,SPI",
    "-Dlog4j2.disable.jmx=true",
    "-Dlog4j2.Script.enableLanguages=nashorn,js,javascript,ecmascript,groovy",
    "-Dmule.bootstrap.container.wrapper.class=org.mule.runtime.module.boot.internal.MuleContainerBasicWrapper",
    "--add-modules=java.se,org.mule.runtime.jpms.utils,com.fasterxml.jackson.core",
    "--add-opens=java.base/java.lang=org.mule.runtime.jpms.utils",
    "--add-opens=java.base/java.lang.reflect=org.mule.runtime.jpms.utils",
    "--add-opens=java.base/java.lang.invoke=org.mule.runtime.jpms.utils",
    "--add-opens=java.sql/java.sql=org.mule.runtime.jpms.utils",
    "-Dpolyglot.engine.WarnInterpreterOnly=false",
)
# The org.mule.boot module (in MULE_HOME/lib/boot) and the class it starts Mule with.
JVM_MAIN = "--module=org.mule.boot/org.mule.runtime.module.reboot.MuleContainerBootstrap"
# The argument that names a runtime's MULE_BASE on its JVM's command line: how a leftover is recognised.
MULE_BASE_ARGUMENT = "-Dmule.base={base}"


class MuleError(Exception):
    """The Mule runtime or the Maven build could not do what was asked."""


class BuildError(MuleError):
    """``mvn package`` failed; ``output`` holds the end of Maven's output."""

    def __init__(self, message: str, output: str) -> None:
        super().__init__(message)
        self.output = output


class DeployError(MuleError):
    """An app did not start; ``log_excerpt`` is the part of mule.log that says why."""

    def __init__(self, message: str, log_excerpt: str) -> None:
        super().__init__(message)
        self.log_excerpt = log_excerpt


class RuntimeStoppedError(DeployError):
    """The Mule runtime itself stopped (or its JVM died) while an app was deploying."""


class RuntimeUnavailableError(MuleError):
    """The local Mule runtime could not be started (or restarted), so no app can be run on it."""


def java_home() -> Path | None:
    """JAVA_HOME, or the installation folder of the ``java`` on PATH, or None."""
    value = os.environ.get("JAVA_HOME")
    if value:
        return Path(value)
    java = shutil.which("java")
    return Path(java).resolve().parent.parent if java is not None else None


def jvm_command(java: Path, mule_home: Path, mule_base: Path) -> list[str]:
    """The command that starts Mule's JVM directly (no Tanuki wrapper), run with MULE_BASE as its folder.

    ``mule_home`` and ``mule_base`` are used as given, so pass absolute paths: ``-Dmule.base`` on this
    command line is also how a JVM left behind under ``mule_base`` is recognised.
    """
    return [
        str(java),
        *JVM_HEAP,
        f"-Dmule.home={mule_home}",
        MULE_BASE_ARGUMENT.format(base=mule_base),
        *JVM_OPTIONS,
        f"--module-path={mule_home / 'lib' / 'boot'}",
        JVM_MAIN,
    ]


def package(project_dir: Path, *, timeout: float = 900.0) -> Path:
    """Build ``project_dir`` with ``mvn package`` and return the one ``target/*-mule-application.jar``."""
    mvn = shutil.which("mvn")
    if mvn is None:
        raise BuildError("Maven (mvn) is not on PATH", "")
    try:
        proc = subprocess.run(
            [mvn, "-B", "-q", "package", "-DskipTests"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BuildError(f"mvn package did not finish within {timeout:.0f}s", _tail(str(exc.output or ""))) from exc
    output = _tail(proc.stdout + proc.stderr)
    if proc.returncode != 0:
        raise BuildError(f"mvn package failed with exit code {proc.returncode}", output)
    jars = sorted((project_dir / "target").glob(f"*{JAR_SUFFIX}"))
    if len(jars) != 1:
        raise BuildError(f"mvn package made {len(jars)} {JAR_SUFFIX} files, expected one", output)
    return jars[0]


def _tail(text: str, lines: int = EXCERPT_LINES) -> str:
    return "\n".join(text.splitlines()[-lines:])


class MuleRunner:
    """One headless Mule runtime under its own MULE_BASE."""

    def __init__(self, mule_home: Path, mule_base: Path) -> None:
        # Absolute: they are passed to the JVM as -Dmule.home and -Dmule.base, which its folder would change.
        self.mule_home = Path(mule_home).absolute()
        self.mule_base = Path(mule_base).absolute()
        # The JVM of the current start: a2m's child, the leader of its own process group, reaped only by stop().
        self._process: subprocess.Popen[bytes] | None = None
        self._pids: list[int] = []

    @property
    def pids(self) -> Sequence[int]:
        """Every PID this runner started for its latest start (the JVM's)."""
        return tuple(self._pids)

    @property
    def log_path(self) -> Path:
        return self.mule_base.joinpath(*MULE_LOG)

    # ------------------------------------------------------------ start and stop

    def start(self, timeout: float = 240.0) -> None:
        """Start Mule under ``mule_base`` and return once it is up; raise MuleError if it is not."""
        if self._process is not None:
            raise MuleError("this Mule runtime is already started")
        try:
            self._stop_leftovers()
            self._prepare_base()
        except (OSError, UnicodeDecodeError) as exc:
            # An unusable install (no services/ folder, e.g. a Mule 3 MULE_HOME; unreadable conf files)
            # is a runtime that cannot start, never a crash of the proxy that needed it.
            raise MuleError(
                f"the Mule runtime under {self.mule_home} cannot be prepared (is it a Mule 4 standalone "
                f"install?): {type(exc).__name__}: {exc}"
            ) from exc
        home = java_home()
        if home is None:
            raise MuleError("Java was not found: set JAVA_HOME or put java on PATH")
        self._keep_log_small()
        # mule.log is kept across runs; an earlier run's "up and kicking" must not count for this one.
        log = LogWatch(self.log_path)
        env = dict(os.environ, MULE_HOME=str(self.mule_home), MULE_BASE=str(self.mule_base), JAVA_HOME=str(home))
        # Appended to, never truncated: a LogWatch made before this start keeps its place in the file.
        log_fd = safefs.open_plain_file(self.mule_base, self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        self._pids = []
        try:
            self._process = subprocess.Popen(
                jvm_command(home / "bin" / "java", self.mule_home, self.mule_base),
                cwd=self.mule_base,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            raise MuleError(f"Java under {home} cannot be run to start Mule: {exc}") from exc
        finally:
            os.close(log_fd)
        self._pids = [self._process.pid]
        _LIVE.add(self)
        self._write_pid_file()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Only this launch's log output counts, and only while its JVM still runs.
            if self._ended():
                break
            if STARTED_SIGNAL in log.text():
                return
            time.sleep(POLL_SECONDS)
        output = _tail(log.text())
        why = self._how_it_ended() if self._ended() else f"did not start within {timeout:.0f}s"
        self.stop()
        raise MuleError(f"Mule {why} under {self.mule_base}:\n{output}")

    def stop(self, timeout: float = 60.0) -> None:
        """Stop Mule and every process it started; safe to call again.

        The JVM's process group gets SIGTERM, then SIGKILL when the JVM has not ended within ``timeout``;
        once the JVM ended (and before it is reaped, so the group is still this runtime's) the group gets a
        last SIGKILL, so nothing the JVM started outlives it.

        When this runner never started its runtime, a leftover an earlier a2m recorded in the base's PID file
        is ended instead (see :meth:`_stop_leftovers`), so a caller that then removes the base never deletes
        the only record of a runtime that is still running."""
        process = self._process
        if process is None:
            self._stop_leftovers()
            return
        self._signal_group(signal.SIGTERM)
        self._wait_ended(timeout)
        if not self._ended():
            self._signal_group(signal.SIGKILL)
            self._wait_ended(10)
        self._signal_group(signal.SIGKILL)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        self._process = None
        _LIVE.discard(self)
        safefs.remove(self.mule_base, self.mule_base / PID_FILE)

    def health_problem(self) -> str | None:
        """Why the started runtime can no longer run apps (its JVM is gone), or None while it is usable."""
        if self._process is None:
            return "the Mule runtime is not running"
        if not self._ended():
            return None
        return f"the Mule runtime's JVM stopped (it {self._how_it_ended()})"

    def _ended(self) -> bool:
        """True once the JVM ended, seen without reaping it, so its PID and process group ID stay this
        runtime's (a zombie holds them) until :meth:`stop` reaps it.

        ``waitid`` with WNOWAIT tells where the OS has it (Linux; macOS from Python 3.13); elsewhere the JVM
        counts as ended once the process table shows it a zombie. Only when neither can tell is it reaped
        to find out, and its group is then no longer signalled."""
        process = self._process
        if process is None or process.returncode is not None:
            return True
        if _waitid is not None:
            try:
                return _waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
            except ChildProcessError:
                pass
        else:
            facts = _facts(process.pid)
            if facts is not None:
                return not facts.running
        return process.poll() is not None

    def _how_it_ended(self) -> str:
        """How the ended JVM ended, in words: its exit code or signal when the OS says so without reaping it."""
        process = self._process
        code = process.returncode if process is not None else None
        if code is None and process is not None and _waitid is not None:
            try:
                info = _waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                info = None
            if info is not None:
                code = info.si_status if info.si_code == os.CLD_EXITED else -info.si_status
        if code is None:
            return "exited"
        return f"was killed by signal {-code}" if code < 0 else f"exited with code {code}"

    def _wait_ended(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._ended():
            time.sleep(POLL_SECONDS)

    def _signal_group(self, sig: signal.Signals) -> None:
        process = self._process
        # The group ID is this runtime's while its leader, the JVM, is not reaped (a zombie still holds it);
        # once reaped it may name an unrelated process's group, so it is not signalled.
        if process is None or process.returncode is not None:
            return
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _write_pid_file(self) -> None:
        if self._process is None:
            return
        data = {"pgid": self._process.pid, "pids": list(self._pids)}
        safefs.write_text_atomic(self.mule_base, self.mule_base / PID_FILE, json.dumps(data) + "\n")

    def _stop_leftovers(self) -> None:
        """End what an earlier runtime under this MULE_BASE left running (its a2m was killed outright).

        Only PIDs recorded in the base's PID file are considered, and only those whose command line names
        this MULE_BASE (``-Dmule.base=<base>``) are signalled; nothing is ever found by name. The recorded
        process group is signalled only while its leader is such a process. Each one is pinned by identity
        when it is chosen, so one that ends while the others stop and whose PID is taken by an unrelated
        process is treated as gone: that process is never waited on or signalled.
        """
        path = self.mule_base / PID_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        recorded = [p for p in (data.get("pids", []) if isinstance(data, dict) else []) if isinstance(p, int)]
        group = data.get("pgid") if isinstance(data, dict) else None
        base = self.mule_base

        def owned(pid: int) -> bool:
            return _runs_under(pid, base)

        pinned = (_pin(pid, owned, recheck=owned) for pid in sorted({p for p in recorded if p > 1}))
        mine = [held for held in pinned if held is not None]
        try:
            leader = [held for held in mine if held.pid == group]
            if isinstance(group, int) and group > 1 and _group_held_by(group, leader):
                try:
                    os.killpg(group, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            for held in mine:
                held.send(signal.SIGTERM)
            deadline = time.monotonic() + LEFTOVER_STOP_SECONDS
            while time.monotonic() < deadline and any(held.alive() for held in mine):
                time.sleep(POLL_SECONDS)
            for held in mine:
                if held.alive():
                    held.send(signal.SIGKILL)
        finally:
            for held in mine:
                held.close()
        safefs.remove(self.mule_base, path)

    def _prepare_base(self) -> None:
        """Create the private MULE_BASE: its own conf (copied), services (linked), apps, domains/default, logs."""
        base = self.mule_base
        base.mkdir(parents=True, exist_ok=True)
        for part in ("conf", "apps", "logs", "services", "domains/default"):
            safefs.make_dirs(base, base / part)
        # Mule loads its services (HTTP, scheduler, DataWeave, ...) from MULE_BASE; they ship
        # exploded under MULE_HOME and are only read, so each one is linked, not copied.
        for service in sorted((self.mule_home / "services").iterdir()):
            link = base / "services" / service.name
            if not os.path.lexists(link):
                os.symlink(service, link, target_is_directory=service.is_dir())
        for source in sorted((self.mule_home / "conf").rglob("*")):
            target = base / "conf" / source.relative_to(self.mule_home / "conf")
            if source.is_dir():
                safefs.make_dirs(base, target)
            elif source.is_file():
                safefs.write_text_atomic(base, target, source.read_text(encoding="utf-8"))

    def _keep_log_small(self) -> None:
        """Move a mule.log that grew past MULE_LOG_KEEP_BYTES to mule.log.1 (replacing it), before a start."""
        try:
            size = self.log_path.lstat().st_size
        except OSError:
            return
        if size > MULE_LOG_KEEP_BYTES:
            safefs.move(self.mule_base, self.log_path, self.log_path.with_name(self.log_path.name + ".1"))

    # ------------------------------------------------------------ deploy and undeploy

    def deploy(
        self, jar: Path, *, app_name: str, timeout: float = 180.0, ports: Sequence[int] | None = None
    ) -> None:
        """Hot-deploy ``jar`` as ``app_name`` and return once it started and serves; raise DeployError if not.

        "Serves" means each HTTP listener port answers with something other than Mule's
        container-level "Server not available" 503 (an app's own 503 counts). The ports are
        ``ports`` (on 127.0.0.1) when given, else the ``http.listener.port`` set in the jar's
        properties files; an app with no listener port is ready once it started. Everything,
        the HTTP probe included, must happen within ``timeout`` seconds.
        """
        if self._process is None:
            raise MuleError("Mule is not started")
        listeners = [("127.0.0.1", int(port)) for port in ports] if ports is not None else _jar_listeners(jar)
        apps = self.mule_base / "apps"
        anchor = apps / f"{app_name}-anchor.txt"
        # An earlier deployment under this name leaves its anchor behind, which would read as this
        # deployment's success; undeploy it completely first, so only the new app can make an anchor.
        if any(os.path.lexists(path) for path in (anchor, apps / app_name, apps / f"{app_name}.jar")):
            self.undeploy(app_name, timeout=timeout)
        if os.path.lexists(anchor):
            raise MuleError(f"the anchor of the earlier {app_name} deployment is still there after undeploying it")
        log = LogWatch(self.log_path)
        partial = apps / f"{app_name}.jar.part"
        safefs.remove(self.mule_base, partial)
        fd = safefs.open_plain_file(self.mule_base, partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, "wb") as out, jar.open("rb") as source:
            shutil.copyfileobj(source, out)
        # Mule only picks up *.jar files, so it never sees a half-copied app.
        safefs.move(self.mule_base, partial, apps / f"{app_name}.jar")
        wait_for_deploy(
            self.mule_base, app_name, timeout=timeout, listeners=listeners, alive=self.health_problem, log=log
        )

    def undeploy(self, app_name: str, *, timeout: float = 60.0) -> None:
        """Undeploy ``app_name`` and wait until Mule removed it; an app that never started is just removed."""
        apps = self.mule_base / "apps"
        anchor = apps / f"{app_name}-anchor.txt"
        app_dir = apps / app_name
        # A runtime that stopped no longer removes undeployed apps: their files are just removed.
        if self._process is not None and anchor.is_file() and self.health_problem() is None:
            safefs.remove(self.mule_base, anchor)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and app_dir.exists():
                time.sleep(POLL_SECONDS)
            if app_dir.exists():
                raise MuleError(f"{app_name} was not undeployed within {timeout:.0f}s")
            return
        for leftover in (apps / f"{app_name}.jar", app_dir, anchor):
            safefs.remove(self.mule_base, leftover)


class LogWatch:
    """The text a log file gained after this watch was made, across rotation and truncation.

    The file's inode and size are recorded when the watch is made, so text that
    was already there (an earlier run or deployment) is never returned. When the
    file is replaced (rolled over), the rest of the old file is read if it is
    still in the same folder, and the new file is read from its start; when it
    shrinks (truncated), it is read from its start.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data = bytearray()
        try:
            info = path.stat()
        except FileNotFoundError:
            self._inode: int | None = None
            self._position = 0
        else:
            self._inode = info.st_ino
            self._position = info.st_size

    def text(self) -> str:
        """Everything written to the log since the watch was made."""
        try:
            handle = self.path.open("rb")
        except FileNotFoundError:
            return self._decoded()
        with handle:
            info = os.fstat(handle.fileno())
            if info.st_ino != self._inode:
                if self._inode is not None:
                    self._finish_rotated(self._inode)
                self._inode, self._position = info.st_ino, 0
            elif info.st_size < self._position:
                self._position = 0
            handle.seek(self._position)
            chunk = handle.read()
        self._position += len(chunk)
        self._data += chunk
        return self._decoded()

    def _finish_rotated(self, inode: int) -> None:
        """Add what the replaced log gained after the watch was made, if it is still in the folder."""
        try:
            entries = list(self.path.parent.iterdir())
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_symlink() or entry.stat().st_ino != inode:
                    continue
                with entry.open("rb") as old:
                    old.seek(self._position)
                    self._data += old.read()
            except OSError:
                continue
            return

    def _decoded(self) -> str:
        return self._data.decode("utf-8", errors="replace")


# A connector built for a newer Mule runtime or Java names an enum constant the runtime does not have
# (seen with mule-http-connector 1.12.x and mule-validation-module 2.0.10 on Mule 4.9.0: JavaVersion.JAVA_25).
VERSION_MISMATCH_SIGNAL = "EnumConstantNotPresentException"
VERSION_MISMATCH_TEXT = "connector version not compatible with Mule runtime {version}"
BOOT_API_JAR = re.compile(r"^mule-module-boot-api-(\d+\.\d+\.\d+[^/]*)\.jar$")
CAUSED_BY = "Caused by:"


def wait_for_deploy(
    mule_base: Path,
    app_name: str,
    *,
    timeout: float,
    listeners: Sequence[tuple[str, int]] = (),
    alive: Callable[[], str | None] | None = None,
    log: LogWatch | None = None,
) -> None:
    """Return once ``app_name`` started under ``mule_base`` (and serves); raise DeployError when not.

    Started: ``apps/<name>-anchor.txt`` exists, or ``Started app '<name>'`` is logged; then each of
    ``listeners`` (host, port) must answer with something other than Mule's container 503. Failed: a
    ``Failed to deploy artifact`` line naming the app is logged. ``alive`` (when given) says why the
    runtime is gone, which raises RuntimeStoppedError. Only log text written after the wait began (or
    after ``log`` was made) counts, so an earlier run or deployment never decides this one. This is
    the one deploy wait; :meth:`MuleRunner.deploy` uses it.
    """
    log = log if log is not None else LogWatch(mule_base.joinpath(*MULE_LOG))
    anchor = mule_base / "apps" / f"{app_name}-anchor.txt"
    started = APP_STARTED_SIGNAL.format(name=app_name)
    waiting = list(listeners)
    deadline = time.monotonic() + timeout
    while True:
        text = log.text()
        failure = _failure_excerpt(text, app_name)
        if failure is not None:
            raise DeployError(f"{app_name} failed to deploy", failure)
        problem = alive() if alive is not None else None
        if problem is not None:
            raise RuntimeStoppedError(f"{problem} while {app_name} was deploying", _tail(text))
        up = anchor.is_file() or started in text
        if up:
            waiting = [address for address in waiting if not _serves(*address)]
            if not waiting:
                return
        if time.monotonic() >= deadline:
            if up:
                shown = ", ".join(str(port) for _, port in waiting)
                raise DeployError(
                    f"{app_name} started but its HTTP listener (port {shown}) did not serve within {timeout:g} seconds",
                    _tail(text),
                )
            raise DeployError(f"{app_name} did not start within {timeout:g} seconds", _tail(text))
        time.sleep(min(DEPLOY_POLL_SECONDS, max(0.0, deadline - time.monotonic())))


def mule_version(mule_home: Path) -> str | None:
    """The version of the Mule runtime installed in ``mule_home`` (from lib/boot), or None when unknown."""
    try:
        names = sorted(entry.name for entry in (mule_home / "lib" / "boot").iterdir())
    except OSError:
        return None
    for name in names:
        match = BOOT_API_JAR.match(name)
        if match is not None:
            return match.group(1)
    return None


def is_version_mismatch(log_excerpt: str) -> bool:
    """True when a deploy failure's log shows a connector built for a newer Mule runtime or Java."""
    return DEPLOY_FAILED_SIGNAL in log_excerpt and VERSION_MISMATCH_SIGNAL in log_excerpt


def deploy_failure_message(app_name: str, log_excerpt: str, *, mule_version: str | None) -> str:
    """A plain-words reason for a failed deployment, read from its mule.log excerpt."""
    runtime = mule_version or "(unknown version)"
    if is_version_mismatch(log_excerpt):
        return (
            f"{app_name} failed to deploy: {VERSION_MISMATCH_TEXT.format(version=runtime)} (a connector in pom.xml "
            f"was built for a newer Mule runtime or Java; use connector versions released for Mule {runtime})"
        )
    causes = [line.split(CAUSED_BY, 1)[1].strip() for line in log_excerpt.splitlines() if CAUSED_BY in line]
    cause = causes[-1] if causes else next(
        (line.strip(" +") for line in log_excerpt.splitlines() if DEPLOY_FAILED_SIGNAL in line), ""
    )
    why = f": {cause}" if cause else ", see the log excerpt"
    return f"{app_name} failed to deploy on Mule runtime {runtime}{why}"


def _failure_excerpt(text: str, app_name: str) -> str | None:
    """The log lines around the first 'Failed to deploy artifact' line naming ``app_name``, or None."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if DEPLOY_FAILED_SIGNAL in line and app_name in line:
            start = max(0, index - 5)
            return "\n".join(lines[start : index + EXCERPT_LINES])
    return None


def _jar_listeners(jar: Path) -> list[tuple[str, int]]:
    """(host to probe, port) of the HTTP listener set in the app jar's top-level properties files."""
    found: list[tuple[str, int]] = []
    try:
        with zipfile.ZipFile(jar) as archive:
            names = sorted(n for n in archive.namelist() if "/" not in n and n.endswith(".properties"))
            texts = [archive.read(name).decode("latin-1") for name in names]
    except (OSError, zipfile.BadZipFile):
        # Not a readable jar: Mule reports the failed deployment itself.
        return found
    for text in texts:
        props = _parse_properties(text)
        port = props.get(LISTENER_PORT_KEY, "")
        if not port.isdigit() or not 0 < int(port) < 65536:
            continue
        host = props.get(LISTENER_HOST_KEY, "")
        address = ("127.0.0.1" if host in ANY_HOSTS else host.strip("[]"), int(port))
        if address not in found:
            found.append(address)
    return found


def _parse_properties(text: str) -> dict[str, str]:
    """The ``key=value`` / ``key: value`` lines of a java.util.Properties text (no line continuations)."""
    props: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#!":
            continue
        cut = min((i for i in (line.find("="), line.find(":")) if i >= 0), default=-1)
        if cut < 0:
            continue
        props[line[:cut].strip()] = line[cut + 1 :].strip()
    return props


def _serves(host: str, port: int) -> bool:
    """True once ``host:port`` answers an HTTP request with anything but Mule's container 503.

    The probe asks ``Expect: 100-continue`` and never sends its one-byte body, so a
    serving Mule answers "100 Continue" without running any flow of the app.
    """
    request = (
        f"GET {PROBE_PATH} HTTP/1.1\r\nHost: {host}:{port}\r\nExpect: 100-continue\r\n"
        "Content-Length: 1\r\nConnection: close\r\n\r\n"
    ).encode("ascii")
    data = b""
    try:
        with socket.create_connection((host, port), timeout=PROBE_SECONDS) as conn:
            conn.sendall(request)
            while len(data) < PROBE_MAX_BYTES:
                head, sep, body = data.partition(b"\r\n\r\n")
                if sep and not _needs_body(head, body):
                    break
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
    except OSError:
        if not data:
            return False
    status_line = data.split(b"\r\n", 1)[0].split()
    if len(status_line) < 2 or not status_line[0].startswith(b"HTTP/") or not status_line[1].isdigit():
        return False
    return not (status_line[1] == b"503" and CONTAINER_UNAVAILABLE in data)


def _needs_body(head: bytes, body: bytes) -> bool:
    """True while a 503 response's body (where the container message would be) is not all read yet."""
    lines = head.split(b"\r\n")
    if len(lines[0].split()) < 2 or lines[0].split()[1] != b"503":
        return False
    for line in lines[1:]:
        length = CONTENT_LENGTH.fullmatch(line)
        if length is not None:
            return len(body) < int(length.group(1))
    return len(body) < len(CONTAINER_UNAVAILABLE)


@dataclass(frozen=True, slots=True)
class _Facts:
    """What a2m checks about a live process before it signals one it did not start in this run."""

    running: bool  # False for a zombie: it has ended
    group: int
    start: str  # when it started, as the OS says it (only compared for equality)
    command: str  # its command line, arguments joined with spaces


class _ProcTable:
    """Process facts from /proc (Linux)."""

    def facts(self, pid: int) -> _Facts | None:
        before = self._stat(pid)
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            return None
        after = self._stat(pid)
        # The start time read on both sides of the command line says all three belong to one process.
        if before is None or after is None or before[19] != after[19]:
            return None
        command = " ".join(os.fsdecode(arg) for arg in argv if arg)
        try:
            return _Facts(running=after[0] != "Z", group=int(after[2]), start=after[19], command=command)
        except ValueError:
            return None

    @staticmethod
    def _stat(pid: int) -> list[str] | None:
        """The fields of /proc/<pid>/stat after the command name (state first), or None when it is gone."""
        try:
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            return None
        return fields if len(fields) > 19 else None


class _PsTable:
    """Process facts from ``ps`` (macOS, and any system without /proc).

    One ``ps -p PID`` per question, with each column its own ``-o`` (POSIX gives a ``=`` header the rest of
    its argument) and the C locale, so the start time (``lstart``, five words such as
    ``Wed Oct  8 07:17:28 2026`` on Linux procps and macOS alike) splits the same way everywhere.
    """

    COLUMNS = ("pgid", "stat", "lstart", "command")
    START_WORDS = 5

    def facts(self, pid: int) -> _Facts | None:
        argv = ["ps", "-ww", *(f"-o{column}=" for column in self.COLUMNS), "-p", str(pid)]
        env = dict(os.environ, LC_ALL="C", LANG="C")
        try:
            done = subprocess.run(argv, capture_output=True, env=env, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        lines = os.fsdecode(done.stdout).splitlines()
        if done.returncode != 0 or len(lines) != 1:
            return None
        words = lines[0].split(maxsplit=2 + self.START_WORDS)
        if len(words) < 2 + self.START_WORDS or not words[0].isdigit():
            return None
        command = words[2 + self.START_WORDS] if len(words) > 2 + self.START_WORDS else ""
        start = " ".join(words[2 : 2 + self.START_WORDS])
        return _Facts(running=not words[1].startswith("Z"), group=int(words[0]), start=start, command=command)


# /proc where the OS has it in Linux's form, else ps; a module attribute so tests can run the ps one on Linux.
_processes: _ProcTable | _PsTable = _ProcTable() if sys.platform.startswith("linux") else _PsTable()


def _facts(pid: int) -> _Facts | None:
    return _processes.facts(pid)


def _names_base(command: str, mule_base: Path) -> bool:
    """True when ``command`` holds the argument ``-Dmule.base=<mule_base>`` (a JVM a2m started there)."""
    marker = MULE_BASE_ARGUMENT.format(base=mule_base)
    return f" {marker} " in f" {command} "


def _runs_under(pid: int, mule_base: Path) -> bool:
    """True when live process ``pid`` is a JVM a2m started under ``mule_base`` (its command line names it)."""
    facts = _facts(pid)
    return facts is not None and facts.running and _names_base(facts.command, mule_base)


# os.pidfd_open where the OS has it (Linux 5.3+); a module attribute so the start-time fallback can be tested.
_pidfd_open: Callable[[int], int] | None = getattr(os, "pidfd_open", None)
# os.waitid, to see that a child exited without reaping it (Linux; macOS from Python 3.13).
_waitid: Callable[[int, int, int], os.waitid_result | None] | None = getattr(os, "waitid", None)


class _Held:
    """One process pinned by identity, so a signal never reaches an unrelated process that later took its PID.

    With a pidfd the process itself is held: it is signalled and watched through the pidfd, which can never
    refer to another process. Without one, its start time (and the ``recheck`` it was pinned with, if any) is
    checked again right before every signal and on every liveness check.
    """

    def __init__(self, pid: int, fd: int | None, start: str | None, recheck: Callable[[int], bool] | None) -> None:
        self.pid = pid
        self._fd = fd
        self._start = start
        self._recheck = recheck

    def alive(self) -> bool:
        """True while the pinned process runs (a zombie counts as ended)."""
        if self._fd is not None:
            # A pidfd becomes readable once its process has exited.
            return not select.select([self._fd], [], [], 0)[0]
        facts = _facts(self.pid)
        if self._start is None or facts is None or facts.start != self._start or not facts.running:
            return False
        return self._recheck is None or self._recheck(self.pid)

    def send(self, sig: signal.Signals) -> None:
        """Send ``sig`` to the pinned process, never to another one that took its PID."""
        if self._fd is not None:
            try:
                signal.pidfd_send_signal(self._fd, sig)
            except (ProcessLookupError, PermissionError):
                pass
        elif self.alive():
            _kill(self.pid, sig)

    def close(self) -> None:
        """Release the pidfd; the process then counts as gone and is never signalled again."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self._start = None


def _pin(pid: int, owned: Callable[[int], bool], *, recheck: Callable[[int], bool] | None = None) -> _Held | None:
    """Pin live process ``pid`` when ``owned(pid)`` holds for it, or None when it is gone or not owned.

    The pidfd is opened before ``owned`` is checked and the process is confirmed still running after, so the
    check was made on the pinned process and not on one that took its PID in between.
    """
    fd: int | None = None
    if _pidfd_open is not None:
        try:
            fd = _pidfd_open(pid)
        except ProcessLookupError:
            return None
        except OSError:
            fd = None  # no pidfd here (old kernel, sandbox): fall back to the start time
    facts = _facts(pid)
    held = _Held(pid, fd, facts.start if facts is not None and facts.running else None, recheck)
    if held._start is None or not owned(pid) or not held.alive():
        held.close()
        return None
    return held


def _group_held_by(group: int, members: Sequence[_Held]) -> bool:
    """True when a live pinned member is in process group ``group``, so that group ID cannot be anyone else's."""
    for held in members:
        facts = _facts(held.pid) if held.alive() else None
        if facts is not None and facts.group == group and held.alive():
            return True
    return False


# Every runtime started and not yet stopped, so the exit hook can stop it whatever ended the program.
_LIVE: set[MuleRunner] = set()


def _stop_all_at_exit() -> None:
    for runner in list(_LIVE):
        try:
            runner.stop(timeout=EXIT_STOP_SECONDS)
        except (MuleError, OSError, subprocess.SubprocessError):
            continue


atexit.register(_stop_all_at_exit)


def _kill(pid: int, sig: signal.Signals) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def failure_excerpt(text: str, app_name: str) -> str | None:
    """The mule.log lines around the first 'Failed to deploy artifact' line naming ``app_name``, or None."""
    return _failure_excerpt(text, app_name)


def log_tail(text: str) -> str:
    """The last lines of a log text, for an excerpt."""
    return _tail(text)
