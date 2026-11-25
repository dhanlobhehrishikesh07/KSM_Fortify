"""Shared pytest setup: make `import app` work when pytest is run from CODE/.

Settings safety: tests must never read or write the developer's real settings
folder or OS keychain, so both are redirected BEFORE `app` is imported."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["KSM_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ksm-test-config-")
os.environ["KSM_DISABLE_KEYRING"] = "1"
for _name in ("GROQ_API_KEY", "GEMINI_API_KEY", "ENABLE_FIREBASE", "FIREBASE_DATABASE_URL"):
    os.environ.pop(_name, None)   # a real key in the shell must not leak into tests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_config_after_test():
    """Tests that change settings must not leak values into the next test."""
    yield
    from app import config
    config.reload()


@pytest.fixture(autouse=True)
def _fresh_shared_instances():
    """The prompt library, the shared settings and the live-configuration hub are process-wide instances (5D wires the AI
    modules to them). A test must not see what an earlier test left in them (for example a patched prompt file)."""
    from app import config_sync, prompt_store, prompt_use, shared_settings
    for reset in (config_sync.reset_hub_for_tests, prompt_store.reset_store_for_tests,
                  shared_settings.reset_shared_for_tests, prompt_use.reset_for_tests):
        reset()
    yield
    for reset in (config_sync.reset_hub_for_tests, prompt_store.reset_store_for_tests,
                  shared_settings.reset_shared_for_tests, prompt_use.reset_for_tests):
        reset()
