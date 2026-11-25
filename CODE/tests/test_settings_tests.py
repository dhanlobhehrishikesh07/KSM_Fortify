"""Phase 3E: the Test buttons. Fake clients only: no network, no real keys."""
import json
from types import SimpleNamespace

from app import settings_tests as st_
from app.settings_store import FileBackend, SettingsStore

KEY = "gsk_abcdefghijklmnop1234"
FB = {"type": "service_account", "project_id": "demo", "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
      "client_email": "b@demo.iam.gserviceaccount.com"}


def store(tmp_path):
    return SettingsStore(directory=tmp_path / "cfg", environ={}, secret_backend=FileBackend(tmp_path / "cfg" / "s.json"))


class GroqFake:
    def __init__(self, ids=("llama-3.3-70b-versatile", "other"), error=None):
        self.ids, self.error = ids, error
        self.models = SimpleNamespace(list=self._list)

    def _list(self):
        if self.error:
            raise self.error
        return SimpleNamespace(data=[SimpleNamespace(id=i) for i in self.ids])


def test_groq_pass_warn_fail_skip(tmp_path):
    s = store(tmp_path)
    assert st_.run_test("groq", s)["status"] == "skip"                      # no key yet
    s.set("GROQ_API_KEY", KEY)
    ok = st_.run_test("groq", s, groq_factory=lambda k: GroqFake())
    assert ok["status"] == "pass" and "available" in ok["message"]
    s.set("GROQ_MODEL_ID", "does-not-exist")
    warn = st_.run_test("groq", s, groq_factory=lambda k: GroqFake())
    assert warn["status"] == "warn" and "does-not-exist" in warn["message"]
    bad = st_.run_test("groq", s, groq_factory=lambda k: GroqFake(error=RuntimeError("Error code: 401 invalid api key")))
    assert bad["status"] == "fail" and "rejected the key" in bad["message"]
    net = st_.run_test("groq", s, groq_factory=lambda k: GroqFake(error=RuntimeError("Connection error.")))
    assert "internet" in net["message"]
    lim = st_.run_test("groq", s, groq_factory=lambda k: GroqFake(error=RuntimeError("429 rate limit")))
    assert "rate limit" in lim["message"] and lim["status"] == "fail"


def test_unsaved_value_is_tested_without_saving_it(tmp_path):
    s = store(tmp_path)
    seen = []
    def factory(k):
        seen.append(k)
        return GroqFake()
    r = st_.run_test("groq", s, {"GROQ_API_KEY": "gsk_typed_but_not_saved_1"}, groq_factory=factory)
    assert r["status"] == "pass" and seen == ["gsk_typed_but_not_saved_1"]
    assert s.get("GROQ_API_KEY") == ""                                      # nothing was saved


def test_the_key_never_appears_in_a_result(tmp_path):
    s = store(tmp_path)
    s.set("GROQ_API_KEY", KEY)
    r = st_.run_test("groq", s, groq_factory=lambda k: GroqFake(error=RuntimeError(f"401 bad key {KEY}")))
    assert KEY not in json.dumps(r) and "***" in r["detail"]


class GeminiFake:
    def __init__(self, missing=(), error=None):
        self.missing, self.error = set(missing), error
        self.models = SimpleNamespace(get=self._get)

    def _get(self, model):
        if self.error:
            raise self.error
        if model in self.missing:
            raise RuntimeError("404 NOT_FOUND model")
        return SimpleNamespace(name=model)


def test_gemini_checks_both_models(tmp_path):
    s = store(tmp_path)
    assert st_.run_test("gemini", s)["status"] == "skip"
    s.set("GEMINI_API_KEY", "AIzaSyABCDEFGHIJKLMNOP9876")
    assert st_.run_test("gemini", s, gemini_factory=lambda k: GeminiFake())["status"] == "pass"
    w = st_.run_test("gemini", s, gemini_factory=lambda k: GeminiFake(missing={"gemini-2.5-flash-lite"}))
    assert w["status"] == "warn" and "Screen scan model" in w["message"]
    f = st_.run_test("gemini", s, gemini_factory=lambda k: GeminiFake(error=RuntimeError("403 PERMISSION_DENIED API_KEY_INVALID")))
    assert f["status"] == "fail" and "rejected the key" in f["message"]


def test_firebase_offline_checks_then_online_check(tmp_path):
    s = store(tmp_path)
    assert st_.run_test("firebase", s)["status"] == "fail"                  # no file yet
    s.import_firebase_text(json.dumps(FB))
    r = st_.run_test("firebase", s, firebase_check=lambda p, u: None)
    assert r["status"] == "warn" and "Database URL is empty" in r["message"]
    s.set("FIREBASE_DATABASE_URL", "https://demo-default-rtdb.firebaseio.com/")
    calls = []
    ok = st_.run_test("firebase", s, firebase_check=lambda p, u: calls.append((p.name, u)))
    assert ok["status"] == "pass" and "demo" in ok["message"] and calls[0][0] == "serviceAccountKey.json"
    bad = st_.run_test("firebase", s, firebase_check=lambda p, u: (_ for _ in ()).throw(RuntimeError("Connection refused")))
    assert bad["status"] == "fail" and "reach" in bad["message"]


def test_firebase_corrupt_file_and_bad_url(tmp_path):
    s = store(tmp_path)
    s.import_firebase_text(json.dumps(FB))
    (tmp_path / "cfg" / "firebase" / "serviceAccountKey.json").write_text("{broken")
    assert st_.run_test("firebase", s)["status"] == "fail"
    s.import_firebase_text(json.dumps(FB))
    r = st_.run_test("firebase", s, {"FIREBASE_DATABASE_URL": "http://insecure"}, firebase_check=lambda p, u: None)
    assert r["status"] == "fail" and "https" in r["message"]


def test_unknown_service():
    assert st_.run_test("nope", None)["status"] == "fail"


def test_a_typed_but_unsaved_key_is_scrubbed_from_results(tmp_path):
    s = store(tmp_path)
    typed = "gsk_typed_never_saved_9999"
    r = st_.run_test("groq", s, {"GROQ_API_KEY": typed},
                     groq_factory=lambda k: GroqFake(error=RuntimeError(f"weird failure {typed}")))
    assert typed not in json.dumps(r)
