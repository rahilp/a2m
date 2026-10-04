"""A mock backend: a small HTTP server on 127.0.0.1 that answers every call and records it.

The generated Mule app under test is pointed at it instead of the proxy's real
target, so a2m never calls a real backend. It listens on 127.0.0.1 only, on a
port the operating system picks. Every call is recorded in arrival order
(method, path, query string, headers with lower-case names, body), safely
under concurrent calls. Answers are, in order of precedence: a one-shot answer
queued with :meth:`MockBackend.enqueue` for that method and path, a standing
answer set with :meth:`MockBackend.respond`, or the default answer. Paths are
matched with the same meaning-preserving normalisation the comparison uses
(:func:`a2m.verify.compare.normal_path`): ``/users/%7Ealice`` and
``/users/~alice`` are one path, ``/orders%2F7`` and ``/orders/7`` are two.
"""

from __future__ import annotations

import socket
import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self
from urllib.parse import urlsplit

from a2m.verify.compare import normal_path
from a2m.verify.model import fold, shout

HOST = "127.0.0.1"
# A request body larger than this is refused (413) instead of read into memory.
MAX_BODY_BYTES = 16 * 1024 * 1024
IDLE_SECONDS = 30.0
METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")


@dataclass(frozen=True, slots=True)
class RecordedCall:
    method: str
    path: str
    query: str
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class MockAnswer:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


class _BodyTooLarge(Exception):
    pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, backend: MockBackend) -> None:
        self.backend = backend
        super().__init__((HOST, 0), _Handler)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(IDLE_SECONDS)
        self.server.backend._opened(self.connection)

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            self.server.backend._closed(self.connection)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _handle(self) -> None:
        try:
            body = self._read_body()
        except _BodyTooLarge:
            self._answer(MockAnswer(413, {"Content-Type": "text/plain"}, b"request body too large"))
            self.close_connection = True
            return
        split = urlsplit(self.path)
        headers: dict[str, str] = {}
        for name, value in self.headers.items():
            key = fold(name)
            headers[key] = f"{headers[key]}, {value}" if key in headers else value
        call = RecordedCall(self.command, split.path, split.query, headers, body)
        self._answer(self.server.backend._record(call))

    def _read_body(self) -> bytes:
        if "chunked" in fold(self.headers.get("Transfer-Encoding") or ""):
            return self._read_chunked()
        length_text = self.headers.get("Content-Length")
        length = int(length_text) if length_text and length_text.strip().isdigit() else 0
        if length > MAX_BODY_BYTES:
            raise _BodyTooLarge
        return self.rfile.read(length) if length else b""

    def _read_chunked(self) -> bytes:
        data = bytearray()
        while True:
            line = self.rfile.readline(1024)
            size = int(line.split(b";", 1)[0].strip() or b"0", 16)
            if size == 0:
                while self.rfile.readline(1024).strip():
                    pass  # trailer lines, up to the blank line that ends the request
                return bytes(data)
            if len(data) + size > MAX_BODY_BYTES:
                raise _BodyTooLarge
            data += self.rfile.read(size)
            self.rfile.readline(1024)

    def _answer(self, answer: MockAnswer) -> None:
        self.send_response(answer.status)
        for name, value in answer.headers.items():
            if fold(name) not in ("content-length", "transfer-encoding", "connection"):
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(answer.body)))
        self.end_headers()
        if self.command != "HEAD" and answer.body:
            self.wfile.write(answer.body)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _handle


class MockBackend:
    """See the module docstring. ``start()`` and ``stop()`` it, or use it as a context manager."""

    def __init__(
        self,
        *,
        default_status: int = 200,
        default_headers: Mapping[str, str] | None = None,
        default_body: bytes = b"",
    ) -> None:
        self._default = MockAnswer(default_status, dict(default_headers or {}), default_body)
        self._lock = threading.Lock()
        self._calls: list[RecordedCall] = []
        self._standing: dict[tuple[str, str], MockAnswer] = {}
        self._queued: dict[tuple[str, str], deque[MockAnswer]] = {}
        self._connections: set[socket.socket] = set()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._port = 0

    # ------------------------------------------------------------ lifecycle

    def start(self) -> Self:
        if self._server is not None:
            raise RuntimeError("the mock backend is already running")
        server = _Server(self)
        self._port = int(server.server_address[1])
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="a2m-mock-backend", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop listening and close open connections; the recorded calls stay readable. Safe to call twice."""
        server, self._server = self._server, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        with self._lock:
            open_connections = list(self._connections)
        for conn in open_connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def host(self) -> str:
        return HOST

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self._port}"

    # ------------------------------------------------------------ calls and answers

    def calls(self) -> list[RecordedCall]:
        """Every call received since the last :meth:`clear`, in arrival order."""
        with self._lock:
            return list(self._calls)

    def clear(self) -> None:
        with self._lock:
            self._calls.clear()

    def respond(
        self, method: str, path: str, *, status: int, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> None:
        """Answer every ``method path`` call (path without the query string) with this, until changed."""
        with self._lock:
            self._standing[(shout(method), normal_path(path))] = MockAnswer(status, dict(headers or {}), body)

    def enqueue(
        self, method: str, path: str, *, status: int, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> None:
        """Answer the next ``method path`` call (path without the query string) with this, once."""
        with self._lock:
            self._queued.setdefault((shout(method), normal_path(path)), deque()).append(
                MockAnswer(status, dict(headers or {}), body)
            )

    def clear_queue(self) -> None:
        """Drop every one-shot answer not used yet."""
        with self._lock:
            self._queued.clear()

    def _record(self, call: RecordedCall) -> MockAnswer:
        key = (shout(call.method), normal_path(call.path))
        with self._lock:
            self._calls.append(call)
            queued = self._queued.get(key)
            if queued:
                return queued.popleft()
            return self._standing.get(key, self._default)

    def _opened(self, conn: socket.socket) -> None:
        with self._lock:
            self._connections.add(conn)

    def _closed(self, conn: socket.socket) -> None:
        with self._lock:
            self._connections.discard(conn)
