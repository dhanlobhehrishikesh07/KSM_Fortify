"""Phase 5F fixes: D-1 (offline edit retried by itself), D-2 (History when Firebase answers with a list),
D-5 (own write echoed back is not 'lost to another computer')."""
import time

import test_prompt_store as tps
import test_shared_settings as tss
from app import prompt_store as ps
from app import shared_settings as sh
from app.cloud_sync import MemoryTreeCloud


class ListyCloud(MemoryTreeCloud):
    """Like the real Firebase: an object whose keys are 1..N without gaps comes back as a list with an empty slot 0."""

    def get(self, path):
        return self._listy(super().get(path))

    @classmethod
    def _listy(cls, node):
        if isinstance(node, dict):
            node = {k: cls._listy(v) for k, v in node.items()}
            if node and all(k.isdigit() for k in node) and set(node) == {str(i) for i in range(1, len(node) + 1)}:
                return [None] + [node[str(i)] for i in range(1, len(node) + 1)]
        return node


# ------------------------------------------------------------------ D-2
def test_history_works_when_firebase_returns_the_versions_as_a_list(tmp_path):
    cloud = ListyCloud(clock=tps.Clock())
    store = tps.make(tmp_path, cloud)
    store.sync()
    for text in ("v1", "v2", "v3"):
        store.save("chat-default", {"identity": {"text": text}})
    assert isinstance(cloud.get(f"{ps.VERSIONS_ROOT}/chat-default"), list)      # the fake now behaves like Firebase
    hist = store.history("chat-default")
    assert hist["ok"] and [v["n"] for v in hist["versions"]] == [3, 2, 1]


# ------------------------------------------------------------------ D-5
def test_our_own_write_arriving_before_the_send_finished_makes_no_notice(tmp_path):
    cloud = MemoryTreeCloud(clock=tss.Clock())
    a = tss.make(tmp_path, cloud)
    a.sync()
    a.view.online = False
    a.store.set("USER_NAME", "Mine")                       # waits in the queue with the old base
    assert a.status()["pending"] == 1
    echo = {"value": "Mine", "updated_at": 1_800_000_999_000, "updated_by": "me"}
    out = a.apply_snapshot({"USER_NAME": echo}, {})        # the listener echo of our own write
    assert out["conflicts"] == [] and a.notices() == []
    assert a.status()["pending"] == 0


def test_a_different_value_from_elsewhere_still_wins_and_is_reported(tmp_path):
    cloud = MemoryTreeCloud(clock=tss.Clock())
    a = tss.make(tmp_path, cloud)
    a.sync()
    a.view.online = False
    a.store.set("USER_NAME", "Mine")
    theirs = {"value": "Theirs", "updated_at": 1_800_000_999_000, "updated_by": "other"}
    out = a.apply_snapshot({"USER_NAME": theirs}, {})
    assert out["conflicts"] == ["USER_NAME"] and [n["mine"] for n in a.notices()] == ["Mine"]


def test_a_slow_marker_read_with_the_live_echo_gives_no_notice(tmp_path):
    cloud = MemoryTreeCloud(clock=tss.Clock())
    a = tss.make(tmp_path, cloud)
    a.sync()
    a.view.keep_server_marker = True
    real_get = a.view.get

    def echoing_get(path):                                  # the SDK thread delivers the echo while we wait for the clock
        node = real_get(path)
        if path.endswith("USER_NAME") and isinstance(node, dict):
            a.apply_snapshot({"USER_NAME": node}, {})
        return node
    a.view.get = echoing_get
    a.store.set("USER_NAME", "Quick")
    a.flush()
    assert a.notices() == [] and a.status()["pending"] == 0


# ------------------------------------------------------------------ D-1
def test_an_offline_edit_goes_up_by_itself_with_real_threads(tmp_path):
    cloud = MemoryTreeCloud(clock=tss.Clock())
    a = tss.make(tmp_path, cloud, background=True)
    a._retry_s, a._max_retry_s = 0.1, 0.4
    a.view.online = False
    a.store.set("USER_NAME", "Later")                       # nobody calls sync() or flush() by hand from here on
    deadline = time.time() + 3
    while time.time() < deadline and a.status()["last_error"] == "":
        time.sleep(0.02)
    assert a.status()["state"] == "offline"
    a.view.online = True
    deadline = time.time() + 5
    while time.time() < deadline and a.status()["pending"]:
        time.sleep(0.05)
    assert a.status()["pending"] == 0 and a.status()["state"] != "offline"
    assert tss.common_node(cloud, "USER_NAME")["value"] == "Later"
    assert a._retry_timer is None


def test_the_retry_pause_grows_and_stops_at_the_limit(tmp_path):
    a = tss.make(tmp_path, MemoryTreeCloud(clock=tss.Clock()), background=True)
    a._retry_s, a._max_retry_s = 30.0, 100.0
    seen = []
    for _ in range(4):
        a._schedule_retry()
        seen.append(a._retry_delay)
    a._cancel_retry()
    assert seen == [30.0, 60.0, 100.0, 100.0] and a._retry_delay == 0.0
