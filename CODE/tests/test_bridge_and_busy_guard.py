"""Phase 2B: Firebase bridge must report failures and mobile messages must not collide with a busy chat."""
import threading
import time
from types import SimpleNamespace

import pytest

from app import config, firebase_bridge
from app.data_store import TaskStore
from app.firebase_bridge import FirebaseBridge, FirebaseBridgeError, run_bridge_safely
from app.gui.web_api import WebAPI


# ---------------------------------------------------------------- helpers
class FakeDB:
    """Stands in for firebase_admin.db: records deletes."""

    def __init__(self):
        self.deleted = []

    def reference(self, path):
        outer = self
        return SimpleNamespace(
            delete=lambda: outer.deleted.append(path),
            set=lambda v: None,
            listen=lambda cb: None,
        )


def _event(data, path="/"):
    return SimpleNamespace(data=data, path=path, event_type="put")


class FakeAssistant:
    available = True

    def __init__(self, gate=None):
        self.gate = gate
        self.seen = []

    def chat(self, history, matrix_json):
        self.seen.append(history[-1])
        if self.gate is not None and len(self.seen) == 1:
            self.gate.wait(5)
        return "reply to " + history[-1].split(": ", 1)[1]

    @staticmethod
    def reply_contains_trigger(reply):
        return False


class FakeScreen:
    available = False

    def problem(self):
        return "n/a"


class FakeOfficer:
    available = False


def _api(tmp_path, assistant):
    return WebAPI(TaskStore(data_file=tmp_path / "m.json"), assistant, FakeOfficer(), FakeScreen())


def _wait(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


# ------------------------------------------------- bridge start errors reach the log
def test_start_without_key_file_raises_clear_error(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_FIREBASE", True)
    monkeypatch.setattr(config, "FIREBASE_SERVICE_ACCOUNT_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(firebase_bridge, "firebase_admin", object())
    with pytest.raises(FirebaseBridgeError, match="not found"):
        FirebaseBridge(on_message=lambda t: None).start()


def test_run_bridge_safely_reports_error_instead_of_losing_it(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_FIREBASE", True)
    monkeypatch.setattr(config, "FIREBASE_SERVICE_ACCOUNT_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(firebase_bridge, "firebase_admin", object())
    lines = []
    ok = run_bridge_safely(FirebaseBridge(on_message=lambda t: None), lines.append)
    assert ok is False
    assert any(l.startswith("[ERROR]") and "not found" in l for l in lines)


def test_run_bridge_safely_reports_unexpected_exception():
    class Boom:
        def start(self):
            raise RuntimeError("network down")

    lines = []
    assert run_bridge_safely(Boom(), lines.append) is False
    assert any("network down" in l for l in lines)


def test_run_bridge_safely_reports_success():
    class Ok:
        def start(self):
            pass

    lines = []
    assert run_bridge_safely(Ok(), lines.append) is True
    assert any("listener started" in l.lower() for l in lines)


# ------------------------------------------------- event handling
def test_initial_snapshot_processes_every_entry_and_deletes_each(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(firebase_bridge, "db", fake)
    got = []
    b = FirebaseBridge(on_message=lambda t: got.append(t))
    b._handle_event(_event({"-a": "buy milk", "-b": "study"}))
    assert got == ["buy milk", "study"]
    assert fake.deleted == ["incoming_tasks/-a", "incoming_tasks/-b"]


def test_child_event_deletes_only_that_child(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(firebase_bridge, "db", fake)
    got = []
    FirebaseBridge(on_message=lambda t: got.append(t))._handle_event(_event("new text", path="/-xyz"))
    assert got == ["new text"]
    assert fake.deleted == ["incoming_tasks/-xyz"]


def test_failing_handler_keeps_message_and_does_not_stop_others(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(firebase_bridge, "db", fake)
    got = []

    def handler(text):
        if text == "bad":
            raise RuntimeError("handler exploded")
        got.append(text)

    FirebaseBridge(on_message=handler)._handle_event(_event({"-a": "bad", "-b": "good"}))
    assert got == ["good"]
    assert fake.deleted == ["incoming_tasks/-b"]  # "bad" stays for a retry on next start


def test_non_text_payload_is_dropped_without_calling_handler(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(firebase_bridge, "db", fake)
    got = []
    FirebaseBridge(on_message=lambda t: got.append(t))._handle_event(_event({"-a": {"x": 1}, "-b": "   "}))
    assert got == []
    assert sorted(fake.deleted) == ["incoming_tasks/-a", "incoming_tasks/-b"]


def test_handler_reply_is_pushed(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(firebase_bridge, "db", fake)
    pushed = []
    b = FirebaseBridge(on_message=lambda t: "hello back")
    b._app = object()
    b.push_reply = pushed.append
    b._handle_event(_event({"-a": "hi"}))
    assert pushed == ["hello back"]


# ------------------------------------------------- busy guard in WebAPI
def test_external_message_gets_processed_and_reply_sent_to_phone(tmp_path):
    api = _api(tmp_path, FakeAssistant())
    sent = []
    api.set_reply_sink(sent.append)
    api.receive_external_message("hello")
    assert _wait(lambda: sent == ["reply to hello"])
    assert _wait(lambda: api._chat_busy is False)


def test_external_message_while_busy_is_queued_then_run_in_order(tmp_path):
    gate = threading.Event()
    assistant = FakeAssistant(gate=gate)
    api = _api(tmp_path, assistant)
    sent = []
    api.set_reply_sink(sent.append)
    api.receive_external_message("first")
    assert _wait(lambda: len(assistant.seen) == 1)        # first is now blocked inside chat()
    api.receive_external_message("second")
    api.receive_external_message("third")
    time.sleep(0.1)
    assert assistant.seen == ["User: first"]              # nothing ran concurrently
    gate.set()
    assert _wait(lambda: sent == ["reply to first", "reply to second", "reply to third"])
    assert _wait(lambda: api._chat_busy is False)


def test_ui_message_is_refused_while_external_message_runs(tmp_path):
    gate = threading.Event()
    api = _api(tmp_path, FakeAssistant(gate=gate))
    api.receive_external_message("first")
    assert _wait(lambda: api._chat_busy)
    assert api.send_chat("from the desktop") == {"ok": False}
    gate.set()
    assert _wait(lambda: api._chat_busy is False)


def test_external_message_with_ai_unavailable_is_reported_not_crashed(tmp_path):
    a = FakeAssistant()
    a.available = False
    api = _api(tmp_path, a)
    api.receive_external_message("hello")
    log = api.get_bridge_log(0)
    assert "AI is not configured" in log
    assert api._chat_busy is False
