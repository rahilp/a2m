"""Shared pytest fixtures for a2m.

Fixture bundles are built under ``tmp_path`` as minimal but valid Apigee
proxies, so they stay valid once later checkpoints add a real parse stage.

Stage contract used by the CP1 tests (the one injection seam):

    a2m.cli.main(argv: list[str] | None = None, *, stages=None) -> int

``stages`` is an optional list of per-proxy callables that replaces the
default pipeline. Each stage is called once per proxy as ``stage(proxy)``,
where ``proxy`` exposes at least ``proxy.name`` (str, the proxy name; for a
zip it is the file stem) and ``proxy.out_dir`` (pathlib.Path, a folder under
``--out`` where this proxy's output goes). A stage that raises marks that
proxy as crashed; the batch carries on with the next proxy.
"""

from __future__ import annotations

import os
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)


@pytest.fixture(autouse=True)
def _no_api_key_and_isolated_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No test ever sees a real API key, and stray relative writes land in tmp_path. No test picks up
    a toolchain installed under the real HOME by ./install.sh --with-mule either (cli.main applies it)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("A2M_NO_TOOLCHAIN", "1")
    monkeypatch.chdir(tmp_path)


def bundle_files(name: str, base_path: str | None = None, target_url: str | None = None) -> dict[str, str]:
    """Return {relative path: text} for a minimal valid Apigee proxy bundle."""
    base_path = base_path if base_path is not None else f"/{name}"
    target_url = target_url if target_url is not None else f"http://127.0.0.1:9/{name}"
    return {
        f"apiproxy/{name}.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<APIProxy revision="1" name="{name}">\n'
            f"    <DisplayName>{name}</DisplayName>\n"
            "    <Description>a2m test fixture</Description>\n"
            "    <ProxyEndpoints>\n"
            "        <ProxyEndpoint>default</ProxyEndpoint>\n"
            "    </ProxyEndpoints>\n"
            "    <TargetEndpoints>\n"
            "        <TargetEndpoint>default</TargetEndpoint>\n"
            "    </TargetEndpoints>\n"
            "    <Policies/>\n"
            "</APIProxy>\n"
        ),
        "apiproxy/proxies/default.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<ProxyEndpoint name="default">\n'
            "    <PreFlow name=\"PreFlow\">\n"
            "        <Request/>\n"
            "        <Response/>\n"
            "    </PreFlow>\n"
            "    <Flows/>\n"
            "    <PostFlow name=\"PostFlow\">\n"
            "        <Request/>\n"
            "        <Response/>\n"
            "    </PostFlow>\n"
            "    <HTTPProxyConnection>\n"
            f"        <BasePath>{base_path}</BasePath>\n"
            "        <VirtualHost>default</VirtualHost>\n"
            "    </HTTPProxyConnection>\n"
            '    <RouteRule name="default">\n'
            "        <TargetEndpoint>default</TargetEndpoint>\n"
            "    </RouteRule>\n"
            "</ProxyEndpoint>\n"
        ),
        "apiproxy/targets/default.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<TargetEndpoint name="default">\n'
            "    <PreFlow name=\"PreFlow\">\n"
            "        <Request/>\n"
            "        <Response/>\n"
            "    </PreFlow>\n"
            "    <Flows/>\n"
            "    <PostFlow name=\"PostFlow\">\n"
            "        <Request/>\n"
            "        <Response/>\n"
            "    </PostFlow>\n"
            "    <HTTPTargetConnection>\n"
            f"        <URL>{target_url}</URL>\n"
            "    </HTTPTargetConnection>\n"
            "</TargetEndpoint>\n"
        ),
    }


def write_bundle_dir(
    parent: Path, name: str, base_path: str | None = None, target_url: str | None = None
) -> Path:
    """Write an unzipped bundle at parent/<name>/apiproxy/... and return parent/<name>."""
    root = parent / name
    for rel, text in bundle_files(name, base_path, target_url).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def write_bundle_zip(
    parent: Path,
    name: str,
    extra_members: Iterable[tuple[str, str]] = (),
    base_path: str | None = None,
    target_url: str | None = None,
) -> Path:
    """Write parent/<name>.zip with apiproxy/ at the zip root (Apigee export layout).

    ``extra_members`` are (member name, text) pairs written verbatim, which is
    how the zip-slip fixtures add unsafe entries.
    """
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / f"{name}.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel, text in bundle_files(name, base_path, target_url).items():
            zf.writestr(zipfile.ZipInfo(rel, FIXED_ZIP_TIME), text)
        for member, text in extra_members:
            zf.writestr(zipfile.ZipInfo(member, FIXED_ZIP_TIME), text)
    return path


@pytest.fixture
def make_bundle() -> Callable[..., Path]:
    """make_bundle(parent, name, base_path=None, target_url=None) -> bundle folder."""
    return write_bundle_dir


@pytest.fixture
def make_zip() -> Callable[..., Path]:
    """make_zip(parent, name, extra_members=(), ...) -> zip path."""
    return write_bundle_zip


@pytest.fixture
def results_dir(tmp_path: Path) -> Path:
    """tmp_path/results. Not created in advance."""
    return tmp_path / "results"


@pytest.fixture
def mixed_exports(tmp_path: Path) -> Path:
    """Exports folder 'mixed': alpha/ (unzipped), beta.zip (zipped), gamma/ (unzipped)."""
    exports = tmp_path / "mixed"
    exports.mkdir()
    write_bundle_dir(exports, "alpha")
    write_bundle_zip(exports, "beta")
    write_bundle_dir(exports, "gamma")
    return exports


@pytest.fixture
def evil_dotdot_zip_member() -> tuple[str, str]:
    return ("apiproxy/../../escaped.txt", "pwned")


@pytest.fixture
def evil_abs_zip_member(tmp_path: Path) -> tuple[str, str]:
    return (str(tmp_path / "abs-escaped.txt"), "pwned")


@pytest.fixture
def broken_zip_bytes() -> bytes:
    """200 bytes of deterministic non-zip garbage."""
    data = (b"this is not a zip file, just garbage. " * 6)[:200]
    assert len(data) == 200
    return data


@dataclass
class Recorder:
    """A per-proxy stage that records which proxies ran and writes one small file."""

    calls: list[str] = field(default_factory=list)

    def __call__(self, proxy: Any) -> None:
        self.calls.append(proxy.name)
        out_dir = Path(proxy.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "recorded.txt").write_text(f"recorded {proxy.name}\n", encoding="utf-8")


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def crashing_stage() -> Callable[[Any], None]:
    """Raises RuntimeError('boom in beta') only for the proxy named beta."""

    def stage(proxy: Any) -> None:
        if proxy.name == "beta":
            raise RuntimeError("boom in beta")

    return stage


@dataclass
class CliResult:
    code: int
    out: str
    err: str


@pytest.fixture
def run_cli(capsys: pytest.CaptureFixture[str]) -> Callable[..., CliResult]:
    """Run a2m.cli.main(argv, stages=...) in-process and capture its output.

    A SystemExit (for example from argparse) is converted to its exit code so
    usage errors can be asserted the same way as returned codes.
    """

    def _run(argv: list[str], stages: list[Callable[[Any], None]] | None = None) -> CliResult:
        from a2m.cli import main

        capsys.readouterr()
        try:
            if stages is None:
                code = main(argv)
            else:
                code = main(argv, stages=stages)
        except SystemExit as exc:  # argparse usage errors and --help
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)
        captured = capsys.readouterr()
        return CliResult(code=code, out=captured.out, err=captured.err)

    return _run


@pytest.fixture
def subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    return env


# ---------------------------------------------------------------- CP1 adversarial round 04 additions
# New imports for the fixture below live here so no existing line changes.

from collections.abc import Iterator  # noqa: E402

STATIC_CHECK_CACHE_VARS = ("MYPY_CACHE_DIR", "RUFF_CACHE_DIR")


@pytest.fixture(scope="session", autouse=True)
def _static_check_caches_under_tmp(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """[CP1-T63] ruff and mypy run by tests keep their caches under pytest's tmp folder, never in the repo.

    Session-scoped, so it is set before any function-scoped fixture (such as
    ``subprocess_env``) copies the environment.
    """
    caches = tmp_path_factory.mktemp("static-check-caches")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MYPY_CACHE_DIR", str(caches / "mypy"))
        patch.setenv("RUFF_CACHE_DIR", str(caches / "ruff"))
        yield caches


# ---------------------------------------------------------------- CP6: the network guard
# New imports for the guard live here so no existing line changes.

import ipaddress  # noqa: E402
import socket  # noqa: E402

GUARD_ALLOWED_NAMES = frozenset({"localhost"})


class BlockedNetworkError(RuntimeError):
    """A test tried to reach a host other than this machine (127.0.0.1, ::1 or localhost)."""


def _guard_host_allowed(host: object) -> bool:
    """True for the loopback addresses and the name localhost; every other name or address is refused without a
    lookup (so 127.0.0.1.example.test is a name, not 127.0.0.1)."""
    text = host.decode("ascii", "replace") if isinstance(host, bytes) else str(host)
    if text.lower().rstrip(".") in GUARD_ALLOWED_NAMES:
        return True
    try:
        return ipaddress.ip_address(text.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _guard_refuse(what: str, host: object, port: object = None) -> BlockedNetworkError:
    target = f"{host}:{port}" if port is not None else f"{host}"
    return BlockedNetworkError(
        f"a2m tests: blocked network access: {what} {target} (only 127.0.0.1, ::1 and localhost are allowed)"
    )


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """[CP6] No test reaches the network: outbound connects and name lookups fail at once with an error naming the
    destination, except to 127.0.0.1, ::1 and the name localhost (the mock backend and the runtime tests use those).
    Checked before any lookup or connect, so nothing leaves the machine."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_sendto = socket.socket.sendto
    real_getaddrinfo = socket.getaddrinfo
    real_gethostbyname = socket.gethostbyname
    real_gethostbyname_ex = socket.gethostbyname_ex
    inet = (socket.AF_INET, socket.AF_INET6)

    def check(sock: socket.socket, address: Any, what: str) -> None:
        if sock.family in inet and isinstance(address, tuple) and address and not _guard_host_allowed(address[0]):
            raise _guard_refuse(what, address[0], address[1] if len(address) > 1 else None)

    def connect(self: socket.socket, address: Any) -> None:
        check(self, address, "connect to")
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        check(self, address, "connect to")
        return real_connect_ex(self, address)

    def sendto(self: socket.socket, data: Any, *args: Any) -> int:
        check(self, args[-1] if args else None, "send to")
        return real_sendto(self, data, *args)

    def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        if host is not None and not _guard_host_allowed(host):
            raise _guard_refuse("name lookup of", host, port)
        return real_getaddrinfo(host, port, *args, **kwargs)

    def gethostbyname(host: str) -> str:
        if not _guard_host_allowed(host):
            raise _guard_refuse("name lookup of", host)
        return real_gethostbyname(host)

    def gethostbyname_ex(host: str) -> Any:
        if not _guard_host_allowed(host):
            raise _guard_refuse("name lookup of", host)
        return real_gethostbyname_ex(host)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", sendto)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", gethostbyname)
    monkeypatch.setattr(socket, "gethostbyname_ex", gethostbyname_ex)
