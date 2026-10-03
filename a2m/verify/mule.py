"""A minimal runner for the local Mule standalone runtime (Mule Kernel CE).

:func:`package` builds a generated project with Maven. :class:`MuleRunner`
starts Mule headless under a private MULE_BASE (so the user's install under
MULE_HOME is only read, never written), hot-deploys built apps into
``<mule_base>/apps``, waits for each to start or fail, undeploys them and
stops Mule again. It records the PID of every process it starts, and
:meth:`MuleRunner.stop` ends all of them.

Deploy signals (proven on Mule 4.9.0): ``apps/<name>-anchor.txt`` appears and
``Started app '<name>'`` is logged when an app started; ``Failed to deploy
artifact`` in ``logs/mule.log`` means it did not. Both start signals can come
slightly before the app's HTTP listener serves: until then Mule's HTTP service
answers every request with a container-level 503 ("Server not available to
handle this request, ..."). So :meth:`MuleRunner.deploy` also probes each
listener port of the app (``http.listener.port`` in the jar's properties) until
Mule answers with anything else.

Java and Maven are found on PATH. When JAVA_HOME is not set, it is derived
from the ``java`` on PATH, since Mule's wrapper starts ``$JAVA_HOME/bin/java``.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import time
import zipfile
from collections.abc import Sequence
from pathlib import Path

from a2m import safefs
from a2m.generator.project import LISTENER_HOST_KEY, LISTENER_PORT_KEY

MULE_LOG = ("logs", "mule.log")
CONSOLE_LOG = ("logs", "console.log")
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
EXCERPT_LINES = 60
# Copied from MULE_HOME into each private MULE_BASE (conf is written to by the launcher).
WRAPPER_ADDITIONAL = "wrapper-additional.conf"
WRAPPER_ADDITIONAL_TEXT = (
    "# Extra JVM settings for this private Mule base; Mule's launcher appends the JVM-specific ones here.\n"
)


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


def java_home() -> Path | None:
    """JAVA_HOME, or the installation folder of the ``java`` on PATH, or None."""
    value = os.environ.get("JAVA_HOME")
    if value:
        return Path(value)
    java = shutil.which("java")
    return Path(java).resolve().parent.parent if java is not None else None


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
        self.mule_home = mule_home
        self.mule_base = mule_base
        self._process: subprocess.Popen[bytes] | None = None
        self._pids: list[int] = []

    @property
    def pids(self) -> Sequence[int]:
        """Every PID this runner started (the launcher script, the wrapper and the JVM)."""
        return tuple(self._pids)

    @property
    def log_path(self) -> Path:
        return self.mule_base.joinpath(*MULE_LOG)

    # ------------------------------------------------------------ start and stop

    def start(self, timeout: float = 240.0) -> None:
        """Start Mule under ``mule_base`` and return once it is up; raise MuleError if it is not."""
        if self._process is not None:
            raise MuleError("this Mule runtime is already started")
        self._prepare_base()
        # mule.log is kept across runs; an earlier run's "up and kicking" must not count for this one.
        log = LogWatch(self.log_path)
        env = dict(os.environ, MULE_HOME=str(self.mule_home), MULE_BASE=str(self.mule_base))
        home = java_home()
        if home is not None:
            env["JAVA_HOME"] = str(home)
        console_fd = safefs.open_plain_file(
            self.mule_base, self.mule_base.joinpath(*CONSOLE_LOG), os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        )
        try:
            self._process = subprocess.Popen(
                [str(self.mule_home / "bin" / "mule"), "console"],
                cwd=self.mule_base,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=console_fd,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            os.close(console_fd)
        self._pids = [self._process.pid]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Only this launch's log output counts, and only while the launcher still runs.
            if self._process.poll() is not None:
                break
            if STARTED_SIGNAL in log.text():
                self._record_started()
                return
            time.sleep(POLL_SECONDS)
        console = _tail(self.mule_base.joinpath(*CONSOLE_LOG).read_text(encoding="utf-8", errors="replace"))
        code = self._process.poll()
        self.stop()
        why = f"exited with code {code}" if code is not None else f"did not start within {timeout:.0f}s"
        raise MuleError(f"Mule {why} under {self.mule_base}:\n{console}")

    def stop(self, timeout: float = 60.0) -> None:
        """Stop Mule and every process it started; safe to call again."""
        process = self._process
        if process is None:
            return
        self._signal_group(signal.SIGTERM)
        for pid in self._pids[1:]:
            _kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self._alive():
            process.poll()
            time.sleep(POLL_SECONDS)
        if self._alive():
            self._signal_group(signal.SIGKILL)
            for pid in [*self._pids, *self._descendants()]:
                _kill(pid, signal.SIGKILL)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        self._process = None

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
            rel = source.relative_to(self.mule_home / "conf")
            target = base / "conf" / rel
            if source.is_dir():
                safefs.make_dirs(base, target)
            elif rel.as_posix() == WRAPPER_ADDITIONAL:
                safefs.write_text_atomic(base, target, WRAPPER_ADDITIONAL_TEXT)
            elif source.is_file():
                safefs.write_text_atomic(base, target, source.read_text(encoding="utf-8"))

    def _descendants(self) -> list[int]:
        """PIDs of the running processes started from the launcher (its whole process tree)."""
        if self._process is None:
            return []
        children: dict[int, list[int]] = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()
            except OSError:
                continue
            # fields after the command: state, ppid, ...; a zombie has already ended.
            if len(fields) > 1 and fields[0] != "Z":
                children.setdefault(int(fields[1]), []).append(int(entry.name))
        found: list[int] = []
        todo = [self._process.pid]
        while todo:
            for child in children.get(todo.pop(), []):
                found.append(child)
                todo.append(child)
        return found

    def _record_started(self) -> None:
        """Record the long-lived processes once Mule is up: the wrapper and the JVM."""
        for pid in self._descendants():
            if pid not in self._pids and _long_lived(pid):
                self._pids.append(pid)

    def _alive(self) -> bool:
        return bool(self._descendants()) or any(_exists(pid) for pid in self._pids)

    def _signal_group(self, sig: signal.Signals) -> None:
        if self._process is None:
            return
        try:
            os.killpg(self._process.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

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
        deadline = time.monotonic() + timeout
        started = APP_STARTED_SIGNAL.format(name=app_name)
        waiting = list(listeners)
        while time.monotonic() < deadline:
            text = log.text()
            failure = _failure_excerpt(text, app_name)
            if failure is not None:
                raise DeployError(f"{app_name} failed to deploy", failure)
            if self._process.poll() is not None:
                raise DeployError(f"Mule stopped while deploying {app_name}", _tail(text))
            if anchor.is_file() and started in text:
                waiting = [address for address in waiting if not _serves(*address)]
                if not waiting:
                    return
            time.sleep(POLL_SECONDS)
        if anchor.is_file() and started in log.text() and waiting:
            shown = ", ".join(str(port) for _, port in waiting)
            raise DeployError(
                f"{app_name} started but its HTTP listener (port {shown}) did not serve within {timeout:.0f}s",
                _tail(log.text()),
            )
        raise DeployError(f"{app_name} did not start within {timeout:.0f}s", _tail(log.text()))

    def undeploy(self, app_name: str, *, timeout: float = 60.0) -> None:
        """Undeploy ``app_name`` and wait until Mule removed it; an app that never started is just removed."""
        apps = self.mule_base / "apps"
        anchor = apps / f"{app_name}-anchor.txt"
        app_dir = apps / app_name
        if self._process is not None and anchor.is_file():
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


def _exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _long_lived(pid: int) -> bool:
    """True for the wrapper and the JVM, not for short helpers the launcher script runs."""
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return False
    return Path(exe).name in ("wrapper", "java")


def _kill(pid: int, sig: signal.Signals) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass
