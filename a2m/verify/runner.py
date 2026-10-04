"""The real runner: build a generated app with Maven and run it on the local Mule runtime, pointed at a mock.

:class:`MuleAppRunner` wraps :mod:`a2m.verify.mule`. :meth:`MuleAppRunner.start`
builds the project with ``mvn package`` in its own folder (a build of the same
sources is reused), starts Mule under the private MULE_BASE the first time it
is needed, and hot-deploys a copy of the built jar whose properties file says
where to listen and where the backend is: the HTTP listener on 127.0.0.1 and a
free port, every target on the mock backend's address. The generated project
itself is never changed.

An app is only started when every outbound HTTP call it makes can be pointed
at the mock backend through those properties (:func:`redirect_plan`): a target
whose address is computed at run time, or any request with its own URL, could
reach a real backend, so such an app is refused.

Use the runner as a context manager (or call :meth:`MuleAppRunner.close`):
closing stops Mule and every process it started, whatever happened.

Before each deploy the runner checks that the runtime is still usable
(:meth:`a2m.verify.mule.MuleRunner.health_problem`). A runtime that stopped
mid-batch (its JVM was killed, crashed or ran out of memory) is stopped
cleanly and started again, once per batch; when it cannot be started (again),
:class:`~a2m.verify.mule.RuntimeUnavailableError` says so for every later app,
which the harness reports as "not run" (static), never as an app failure. A
runtime that dies while an app's tests run is seen through
:meth:`MuleAppRunner.runtime_problem`: the harness stops that app's tests and
says the runtime stopped, and the next app gets the restart.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import re
import socket
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from a2m import safefs
from a2m.runlog import get_logger
from a2m.verify.model import AppUnderTest, HttpRequest, HttpResponse
from a2m.verify.mule import (
    BuildError,
    DeployError,
    MuleError,
    MuleRunner,
    RuntimeUnavailableError,
    mule_version,
    package,
)

HTTP_NS = "http://www.mulesoft.org/schema/mule/http"
MULE_DIR = ("src", "main", "mule")
PROPERTIES_ENTRY = "config.properties"
PLACEHOLDER = re.compile(r"\$\{([^${}]+)\}")
LOOPBACK = "127.0.0.1"
BUILDS_DIR = "a2m-builds"
BUILD_TIMEOUT = 900.0
START_TIMEOUT = 240.0
DEPLOY_TIMEOUT = 180.0
UNDEPLOY_TIMEOUT = 60.0
STOP_TIMEOUT = 60.0
SEND_TIMEOUT = 60.0
SKIPPED_PARTS = frozenset({"target", ".mvn"})
# How often one batch restarts a Mule runtime that stopped under it.
MAX_RESTARTS = 1


class UnsafeAppError(MuleError):
    """The app could call an address a2m cannot point at the mock backend, so it is not run."""


@dataclass(frozen=True, slots=True)
class RedirectPlan:
    """The properties that point an app at the mock backend: listener host and port keys, each target's keys."""

    listener_hosts: tuple[str, ...]
    listener_ports: tuple[str, ...]
    target_protocols: tuple[str, ...]
    target_hosts: tuple[str, ...]
    target_ports: tuple[str, ...]


def _pure_key(value: str | None) -> str | None:
    match = PLACEHOLDER.fullmatch((value or "").strip())
    return match.group(1).strip() if match is not None else None


def redirect_plan(app_dir: Path) -> RedirectPlan | str:
    """How to point ``app_dir``'s app at a mock backend through its properties, or why that is not possible."""
    listener_hosts: list[str] = []
    listener_ports: list[str] = []
    protocols: list[str] = []
    hosts: list[str] = []
    ports: list[str] = []
    files = sorted(app_dir.joinpath(*MULE_DIR).glob("*.xml"))
    if not files:
        return f"the project has no Mule configuration in {'/'.join(MULE_DIR)}"
    for path in files:
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError) as exc:
            return f"{path.name} cannot be read: {exc}"
        for conn in root.iter(f"{{{HTTP_NS}}}listener-connection"):
            host, port = _pure_key(conn.get("host")), _pure_key(conn.get("port"))
            if host is None or port is None:
                return f"an HTTP listener in {path.name} does not take its host and port from the properties file"
            listener_hosts.append(host)
            listener_ports.append(port)
        for config in root.iter(f"{{{HTTP_NS}}}request-config"):
            connection = config.find(f"{{{HTTP_NS}}}request-connection")
            keys = [_pure_key(connection.get(a)) if connection is not None else None for a in ("protocol", "host", "port")]
            protocol, host, port = keys
            if protocol is None or host is None or port is None:
                return (
                    f"the HTTP request configuration {config.get('name', '')!r} in {path.name} computes its address "
                    "at run time, so a2m cannot point it at the mock backend"
                )
            protocols.append(protocol)
            hosts.append(host)
            ports.append(port)
        for request in root.iter(f"{{{HTTP_NS}}}request"):
            if request.get("url") is not None or request.get("config-ref") is None:
                return f"an HTTP request in {path.name} names its own URL, so a2m cannot point it at the mock backend"
    return RedirectPlan(
        tuple(dict.fromkeys(listener_hosts)),
        tuple(dict.fromkeys(listener_ports)),
        tuple(dict.fromkeys(protocols)),
        tuple(dict.fromkeys(hosts)),
        tuple(dict.fromkeys(ports)),
    )


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK, 0))
        return int(sock.getsockname()[1])


def _escape_property(text: str, *, key: bool) -> str:
    out: list[str] = []
    for index, char in enumerate(text):
        if char in "\\=:#!" or (char == " " and (key or index == 0)):
            out.append("\\" + char)
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif ord(char) < 0x20 or ord(char) > 0x7E:
            out.extend(f"\\u{unit:04x}" for unit in _utf16_units(char))
        else:
            out.append(char)
    return "".join(out)


def _utf16_units(char: str) -> list[int]:
    data = char.encode("utf-16-be")
    return [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2)]


def _property_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped[0] in "#!":
        return None
    cut = min((i for i in (stripped.find("="), stripped.find(":")) if i >= 0), default=-1)
    return (stripped[:cut] if cut >= 0 else stripped).strip()


def override_properties(text: str, settings: Mapping[str, str]) -> str:
    """``text`` (a properties file) with every key of ``settings`` set to its value, other lines unchanged."""
    lines = [line for line in text.splitlines() if _property_key(line) not in settings]
    lines += [f"{_escape_property(k, key=True)}={_escape_property(v, key=False)}" for k, v in settings.items()]
    return "\n".join(lines) + "\n"


def _fingerprint(app_dir: Path) -> str:
    """A hash of every file of the project (not target/), so a rebuild of unchanged sources is skipped."""
    digest = hashlib.sha256()
    for path in _project_files(app_dir):
        rel = path.relative_to(app_dir).as_posix()
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _project_files(app_dir: Path) -> Iterator[Path]:
    for folder, dirs, files in os.walk(app_dir):
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED_PARTS and not Path(folder, d).is_symlink())
        for name in sorted(files):
            path = Path(folder, name)
            if path.is_file() and not path.is_symlink():
                yield path


class MuleAppHandle:
    """An app deployed on the runner's Mule runtime, listening on 127.0.0.1:``port``."""

    def __init__(self, runner: MuleAppRunner, name: str, port: int) -> None:
        self._runner = runner
        self._name = name
        self._port = port
        self._stopped = False

    @property
    def running(self) -> bool:
        return not self._stopped

    @property
    def base_url(self) -> str | None:
        return f"http://{LOOPBACK}:{self._port}"

    def send(self, request: HttpRequest) -> HttpResponse:
        if self._stopped:
            raise MuleError(f"{self._name} is no longer deployed")
        conn = http.client.HTTPConnection(LOOPBACK, self._port, timeout=SEND_TIMEOUT)
        try:
            conn.request(request.method, request.path, body=request.body or None, headers=dict(request.headers))
            got = conn.getresponse()
            body = got.read()
            headers: dict[str, str] = {}
            for name, value in got.getheaders():
                headers[name] = f"{headers[name]}, {value}" if name in headers else value
            return HttpResponse(got.status, headers, body)
        except (OSError, http.client.HTTPException) as exc:
            raise MuleError(f"no valid HTTP response from {self._name}: {type(exc).__name__}: {exc}") from exc
        finally:
            conn.close()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._runner._undeploy(self._name)


class MuleAppRunner:
    """The real Runner (see the module docstring)."""

    def __init__(self, mule_home: Path, mule_base: Path) -> None:
        self.mule_home = Path(mule_home).absolute()
        self.mule_base = Path(mule_base).absolute()
        self._mule = MuleRunner(mule_home=self.mule_home, mule_base=self.mule_base)
        self._started = False
        self._start_error: RuntimeUnavailableError | None = None
        self._restarts = 0
        self._builds: dict[str, Path] = {}
        self._deploys = 0
        self.mule_version = mule_version(self.mule_home)

    @property
    def pids(self) -> Sequence[int]:
        return self._mule.pids

    def runtime_problem(self) -> str | None:
        """Why the runtime started for this batch stopped being usable (its JVM died, ...), or None while it runs.

        The harness asks this when a call to an app gets no answer, so a runtime that died during the tests
        is reported as such, not as a broken app; the next :meth:`start` restarts it (once per batch).
        """
        if not self._started:
            return None
        return self._mule.health_problem()

    def restart_available(self) -> bool:
        """Whether the next :meth:`start` would restart a runtime that stopped (it restarts once per batch)."""
        return self._start_error is None and self._restarts < MAX_RESTARTS

    @property
    def unavailable_reason(self) -> str | None:
        """Why no app can run on this runner any more (the runtime did not start or restart), or None."""
        return str(self._start_error) if self._start_error is not None else None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Stop Mule and every process it started; safe to call twice."""
        self._mule.stop(timeout=STOP_TIMEOUT)
        self._started = False

    def start(self, app: AppUnderTest, *, backend_url: str) -> MuleAppHandle:
        """Build ``app``, start Mule if needed and deploy the app pointed at ``backend_url``; see the module docstring.

        Raises BuildError (the build failed), DeployError (the app did not start; RuntimeStoppedError when
        the runtime itself stopped meanwhile), UnsafeAppError (it could call a real backend) or
        RuntimeUnavailableError (the runtime did not start, or stopped and could not be restarted).
        """
        plan = redirect_plan(app.app_dir)
        if isinstance(plan, str):
            raise UnsafeAppError(f"{app.name} is not run: {plan}")
        jar = self._build(app.app_dir)
        self._ensure_started()
        port = free_port()
        backend = _backend_address(backend_url)
        settings: dict[str, str] = dict(app.properties)
        settings.update({key: LOOPBACK for key in plan.listener_hosts})
        settings.update({key: str(port) for key in plan.listener_ports})
        settings.update({key: "HTTP" for key in plan.target_protocols})
        settings.update({key: backend[0] for key in plan.target_hosts})
        settings.update({key: str(backend[1]) for key in plan.target_ports})
        deployed = self._configured_copy(jar, app.name, settings)
        try:
            self._mule.deploy(deployed, app_name=app.name, timeout=DEPLOY_TIMEOUT, ports=[port])
        except DeployError:
            self._undeploy(app.name, quiet=True)
            raise
        finally:
            safefs.remove(self.mule_base, deployed)
        return MuleAppHandle(self, app.name, port)

    # ------------------------------------------------------------ helpers

    def _build(self, app_dir: Path) -> Path:
        key = _fingerprint(app_dir)
        cached = self._builds.get(key)
        if cached is not None and cached.is_file():
            return cached
        jar = package(app_dir, timeout=BUILD_TIMEOUT)
        builds = self.mule_base / BUILDS_DIR
        self.mule_base.mkdir(parents=True, exist_ok=True)
        safefs.make_dirs(self.mule_base, builds)
        copy = builds / f"build-{len(self._builds) + 1}.jar"
        safefs.remove(self.mule_base, copy)
        fd = safefs.open_plain_file(self.mule_base, copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, "wb") as out, jar.open("rb") as source:
            while chunk := source.read(1 << 20):
                out.write(chunk)
        self._builds[key] = copy
        return copy

    def _ensure_started(self) -> None:
        """Start the runtime on first use; restart it (once per batch) when it stopped since."""
        if self._start_error is not None:
            raise RuntimeUnavailableError(str(self._start_error))
        what = "start"
        if self._started:
            problem = self._mule.health_problem()
            if problem is None:
                return
            self._started = False
            self._mule.stop(timeout=STOP_TIMEOUT)
            if self._restarts >= MAX_RESTARTS:
                self._start_error = RuntimeUnavailableError(
                    f"the local Mule runtime stopped again ({problem}) after a restart, so it is not restarted "
                    "any more in this run"
                )
                raise RuntimeUnavailableError(str(self._start_error))
            self._restarts += 1
            get_logger().warning("%s; restarting the local Mule runtime under %s", problem, self.mule_base)
            what = f"restart after it stopped ({problem})"
        try:
            self._mule.start(timeout=START_TIMEOUT)
        except MuleError as exc:
            self._start_error = RuntimeUnavailableError(f"the local Mule runtime did not {what}: {exc}")
            raise RuntimeUnavailableError(str(self._start_error)) from exc
        self._started = True

    def _configured_copy(self, jar: Path, app_name: str, settings: Mapping[str, str]) -> Path:
        """A copy of ``jar`` whose properties file has ``settings``, for deploying (the build stays as it was)."""
        self._deploys += 1
        target = self.mule_base / BUILDS_DIR / f"deploy-{self._deploys}.jar"
        safefs.remove(self.mule_base, target)
        try:
            with zipfile.ZipFile(jar) as source:
                names = source.namelist()
                if PROPERTIES_ENTRY not in names:
                    raise BuildError(f"the built app has no {PROPERTIES_ENTRY}", "")
                fd = safefs.open_plain_file(self.mule_base, target, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                with os.fdopen(fd, "wb") as raw, zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as out:
                    for info in source.infolist():
                        data = source.read(info)
                        if info.filename == PROPERTIES_ENTRY:
                            data = override_properties(data.decode("latin-1"), settings).encode("latin-1")
                        out.writestr(info, data)
        except (OSError, zipfile.BadZipFile) as exc:
            raise BuildError(f"the built jar of {app_name} cannot be read: {exc}", "") from exc
        return target

    def _undeploy(self, name: str, *, quiet: bool = False) -> None:
        try:
            self._mule.undeploy(name, timeout=UNDEPLOY_TIMEOUT)
        except MuleError:
            if not quiet:
                raise
        apps = self.mule_base / "apps"
        for leftover in (apps / f"{name}.jar", apps / f"{name}.jar.part"):
            safefs.remove(self.mule_base, leftover)


def _backend_address(url: str) -> tuple[str, int]:
    match = re.fullmatch(r"http://([^/:]+):(\d+)/?", url.strip())
    if match is None:
        raise MuleError(f"the mock backend address {url!r} is not http://host:port")
    return match.group(1), int(match.group(2))
