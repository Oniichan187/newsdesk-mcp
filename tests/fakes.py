"""Test doubles: a real-TCP fake Discord server and a live uvicorn runner."""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx2 as httpx
import uvicorn


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class FakeDiscord:
    """Speaks just enough of the webhook API. mode: ok | slow | 429 | 500."""

    def __init__(self) -> None:
        self.mode = "ok"
        self.received: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self.deleted: list[str] = []
        self._next_id = 1100000000000000000
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.paths.append(self.path)
                outer.received.append(json.loads(body))
                if outer.mode == "slow":
                    time.sleep(3)
                if outer.mode == "429":
                    self._json(429, {"message": "rate limited", "retry_after": 1.0, "global": False})
                    return
                if outer.mode == "500":
                    self._json(500, {"message": "oops"})
                    return
                outer._next_id += 1
                self._json(200, {"id": str(outer._next_id), "content": "..."})

            def do_DELETE(self) -> None:
                outer.deleted.append(self.path.rsplit("/", 1)[-1])
                self.send_response(204)
                self.end_headers()

            def _json(self, status: int, obj: dict[str, Any]) -> None:
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except BrokenPipeError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def client(self, read_timeout: float = 20.0) -> httpx.Client:
        """httpx client whose transport redirects discord.com traffic to this fake over real TCP."""
        port = self.port

        class Redirect(httpx.HTTPTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=port)
                return super().handle_request(request)

        return httpx.Client(
            transport=Redirect(), timeout=httpx.Timeout(connect=2.0, read=read_timeout, write=2.0, pool=2.0)
        )

    def close(self) -> None:
        self.server.shutdown()


class LiveServer:
    def __init__(self, app: Any, port: int) -> None:
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False)
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.05)
        self.base = f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


class CountingListener:
    """Counts TCP connections; proves the relay never fetches caller-supplied URLs."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        self._stop = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while not self._stop:
            try:
                c, _ = self.sock.accept()
                self.accepted += 1
                c.close()
            except OSError:
                continue

    def close(self) -> None:
        self._stop = True
        self.sock.close()
