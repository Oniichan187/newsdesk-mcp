"""Gateway presence against a real local WebSocket fake of the Discord Gateway."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable

import pytest
from websockets.sync.client import connect
from websockets.sync.server import serve

from newsrelay.publishing.presence import FATAL_CLOSE_CODES, Presence

TOKEN = "MTAwMDAwMDAwMDAwMDAwMDAwMA.GaBcDe.abcdefghijklmnopqrstuvwxyz0123456789ABCD"


class FakeGateway:
    """Scriptable gateway. `mode`: normal | reconnect | zombie | invalid | fatal."""

    def __init__(self, mode: str = "normal", interval_ms: int = 150) -> None:
        self.mode = mode
        self.interval_ms = interval_ms
        self.received: list[dict] = []
        self.connections = 0
        self.lock = threading.Lock()
        self.server = serve(self._handler, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _handler(self, ws) -> None:
        with self.lock:
            self.connections += 1
            n = self.connections
        ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": self.interval_ms}}))
        first = json.loads(ws.recv())
        self.received.append(first)
        if self.mode == "fatal":
            ws.close(4004, "Authentication failed.")
            return
        if first["op"] == 2:
            if self.mode == "invalid" and n == 1:
                ws.send(json.dumps({"op": 9, "d": False}))
            else:
                ws.send(json.dumps({"op": 0, "t": "READY", "s": 1,
                                    "d": {"session_id": "sess1", "resume_gateway_url": self.url}}))  # fmt: skip
        elif first["op"] == 6:
            ws.send(json.dumps({"op": 0, "t": "RESUMED", "s": first["d"]["seq"] + 1, "d": None}))
        beats = 0
        try:
            while True:
                msg = json.loads(ws.recv())
                self.received.append(msg)
                if msg["op"] == 1:
                    beats += 1
                    if self.mode != "zombie" or n > 1:
                        ws.send(json.dumps({"op": 11}))
                    if self.mode == "reconnect" and n == 1 and beats == 2:
                        ws.send(json.dumps({"op": 7, "d": None}))
        except Exception:
            return

    def ops(self) -> list[int]:
        return [m["op"] for m in self.received]

    def close(self) -> None:
        self.server.shutdown()


def run_until(p: Presence, cond: Callable[[], bool], timeout: float = 8.0) -> None:
    t = threading.Thread(target=p.run_forever, daemon=True)
    t.start()
    deadline = time.time() + timeout
    while time.time() < deadline and not cond():
        time.sleep(0.05)
    p.stop.set()
    t.join(5)
    assert not t.is_alive()


def make(gw: FakeGateway, tmp_path, notes: list[str]) -> Presence:
    return Presence(TOKEN, activity_name="the news", runtime_dir=tmp_path, gateway_url=gw.url,
                    connector=lambda url: connect(url, open_timeout=5, ping_interval=None),
                    notify=notes.append)  # fmt: skip


def test_identify_payload_and_online_state(tmp_path):
    gw = FakeGateway()
    notes: list[str] = []
    p = make(gw, tmp_path, notes)
    run_until(p, lambda: gw.ops().count(1) >= 2)
    ident = gw.received[0]
    assert ident["op"] == 2
    assert ident["d"]["token"] == TOKEN and ident["d"]["intents"] == 0
    assert ident["d"]["presence"]["status"] == "online"
    assert ident["d"]["presence"]["activities"] == [{"name": "the news", "type": 3}]
    assert "ready" in p.session.events
    assert notes.count("READY=1") == 1 and "WATCHDOG=1" in notes
    gw.close()


def test_reconnect_request_resumes_session(tmp_path):
    gw = FakeGateway("reconnect")
    p = make(gw, tmp_path, [])
    run_until(p, lambda: "resumed" in p.session.events)
    resume = next(m for m in gw.received if m["op"] == 6)
    assert resume["d"]["session_id"] == "sess1" and resume["d"]["seq"] == 1
    assert gw.connections == 2
    gw.close()


def test_zombie_connection_detected(tmp_path):
    gw = FakeGateway("zombie")
    p = make(gw, tmp_path, [])
    run_until(p, lambda: gw.connections >= 2 and "resumed" in p.session.events)
    assert any(e.startswith("reconnect:no heartbeat ACK") for e in p.session.events)
    gw.close()


def test_invalid_session_reidentifies(tmp_path):
    gw = FakeGateway("invalid")
    p = make(gw, tmp_path, [])
    run_until(p, lambda: "ready" in p.session.events, timeout=12)
    assert gw.ops().count(2) == 2 and 6 not in gw.ops()
    gw.close()


def test_fatal_close_code_pauses_instead_of_hammering(tmp_path):
    gw = FakeGateway("fatal")
    p = make(gw, tmp_path, [])
    run_until(p, lambda: any(e.startswith("fatal:") for e in p.session.events))
    time.sleep(0.5)
    assert gw.connections == 1  # no retry storm
    assert 4004 in FATAL_CLOSE_CODES
    gw.close()


def test_unreachable_gateway_backs_off(tmp_path):
    notes: list[str] = []
    p = Presence(TOKEN, runtime_dir=tmp_path, gateway_url="ws://127.0.0.1:9",
                 connector=lambda url: connect(url, open_timeout=1), notify=notes.append)  # fmt: skip
    run_until(p, lambda: p.session.fails >= 1, timeout=5)
    assert notes[0] == "READY=1"  # offline at boot is not a failed start
    assert (tmp_path / "presence.state").read_text().startswith("offline")


def test_token_never_in_repr_or_logs(tmp_path, caplog):
    p = Presence(TOKEN, runtime_dir=tmp_path, gateway_url="ws://127.0.0.1:9",
                 connector=lambda url: (_ for _ in ()).throw(OSError(f"fail {TOKEN}")), notify=lambda m: None)  # fmt: skip
    run_until(p, lambda: p.session.fails >= 1, timeout=5)
    assert TOKEN not in repr(p) and TOKEN not in caplog.text


@pytest.mark.parametrize("enabled", [False, True])
def test_main_without_token_exits_cleanly(cfg, enabled):
    import dataclasses

    from newsrelay.publishing import presence

    c = dataclasses.replace(cfg, discord_presence=enabled)
    assert presence.main(c) == 0  # no bot token configured in the test secrets dir
