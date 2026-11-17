import json
import os
import tempfile

import pytest

# Paths are fixed at import, so point Roosta at a throwaway workspace before anything loads it.
# Otherwise the runner's seeded configs (which mirror the user's MCP config) land in the repo.
os.environ["ROOSTA_WORKSPACE"] = tempfile.mkdtemp(prefix="roosta-tests-")

from roosta import ai_backend


@pytest.fixture(autouse=True)
def pinned_backend(tmp_path, monkeypatch):
    """Tests default to Cursor whatever the live dashboard toggle says."""
    path = tmp_path / "ai-backend.json"
    path.write_text(json.dumps({"backend": ai_backend.CURSOR}))
    monkeypatch.setattr(ai_backend, "BACKEND_FILE", path)
    return path


@pytest.fixture(autouse=True)
def isolated_activity_log(tmp_path, monkeypatch):
    """Test runs must not append fake runs to the live activity feed."""
    from roosta import app

    path = tmp_path / "activity.jsonl"
    monkeypatch.setattr(app, "ACTIVITY_FILE", path)
    return path


@pytest.fixture(autouse=True)
def test_profile(monkeypatch):
    """Prompts read the user's profile; tests use a fixed one, never the real PROFILE.md."""
    from roosta import profile

    me = profile.Profile(name="Sam Example", language="British English")
    monkeypatch.setattr(profile, "load", lambda path=None: me)
    return me
