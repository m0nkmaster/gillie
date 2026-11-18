import json

import pytest

from roosta import task_config
from roosta import github_issues as issues


def test_grant_is_bound_to_connection_task_and_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(issues.ai_connections, "CONNECTIONS_DIR", tmp_path)
    conn = "a" * 32
    directory = tmp_path / conn
    directory.mkdir()
    grant = directory / "issue-grants.json"
    grant.write_text(json.dumps({"worker": {"skill": "engineer", "repository": "owner/repo"}}))
    assert issues.approved_repo(conn, "worker", "engineer") == "owner/repo"
    assert not issues.approved_repo("b" * 32, "worker", "engineer")
    assert not issues.approved_repo(conn, "other", "engineer")
    assert not issues.approved_repo(conn, "worker", "other")
    grant.unlink()
    assert not issues.approved_repo(conn, "worker", "engineer")


@pytest.mark.parametrize("change", [{"pull_request": {}}, {"labels": []}, {"state": "closed"},
                                    {"html_url": "https://github.com/other/repo/issues/1"}])
def test_rejects_ineligible_target_before_writing(change, monkeypatch):
    def api(endpoint):
        assert endpoint == "repos/owner/repo/issues/1"
        return json.dumps({"html_url": "https://github.com/owner/repo/issues/1",
                           "state": "open", "labels": [{"name": "ready"}], **change})
    monkeypatch.setattr(issues, "_api", api)
    with pytest.raises(ValueError, match="open ready issue"):
        issues.apply_update("owner/repo", {"issue": 1, "comment": "Question?", "complete": False})


def test_clarification_and_retry_after_label_failure(monkeypatch):
    comments = []
    labels = ["ready"]
    deny_label = True

    def api(endpoint, *args, payload=None):
        if endpoint.endswith("/comments"):
            if payload:
                comments.append(payload["body"])
            return "\n".join(json.dumps(comment) for comment in comments)
        if endpoint.endswith("/labels"):
            assert payload == {"labels": ["in review"]}
            if deny_label:
                raise ValueError("label failed")
            labels.append("in review")
            return json.dumps([{"name": name} for name in labels])
        return json.dumps({"html_url": "https://github.com/owner/repo/issues/1",
                           "state": "open", "labels": [{"name": name} for name in labels]})
    monkeypatch.setattr(issues, "_api", api)
    update = {"issue": 1, "complete": False, "comment": "Which screen?"}
    issues.apply_update("owner/repo", update)
    assert labels == ["ready"]
    update.update(complete=True, comment="Fixed in abc.\nTests passed.")
    with pytest.raises(ValueError, match="label failed"):
        issues.apply_update("owner/repo", update)
    deny_label = False
    issues.apply_update("owner/repo", update)
    assert labels == ["ready", "in review"]
    assert comments == ["Which screen?", "Fixed in abc.\nTests passed."]


@pytest.mark.parametrize("extra", [{"issue": True}, {"issue": "../other"}, {"complete": "false"},
                                  {"repository": "other/repo"}, {"label": "ready"}, {"comment": ""}])
def test_invalid_update_never_calls_github(extra, monkeypatch):
    monkeypatch.setattr(issues, "_api", lambda *a: pytest.fail("Unexpected API request"))
    with pytest.raises(ValueError):
        issues.apply_update("owner/repo", {"issue": 1, "complete": False, "comment": "ok", **extra})


@pytest.mark.parametrize("revoked", [False, True])
def test_connection_run_applies_only_a_current_grant(monkeypatch, tmp_path, revoked):
    import asyncio
    from types import SimpleNamespace
    from roosta import app

    conn = "a" * 32
    monkeypatch.setattr(issues.ai_connections, "CONNECTIONS_DIR", tmp_path)
    directory = tmp_path / conn
    directory.mkdir()
    grant = directory / "issue-grants.json"
    grant.write_text(json.dumps({"ada": {"skill": "engineer", "repository": "owner/repo"}}))
    connection = {"id": conn, "provider": "anthropic"}
    run = SimpleNamespace(connection=connection, session_id="session", text=lambda s: s)
    monkeypatch.setattr(app.ai_connections, "get", lambda _: connection)
    monkeypatch.setattr(app.ai_backend, "prepare", lambda *args, **kwargs: run)
    update = {"issue": 5, "comment": "Tested.", "complete": True}

    async def stream(run, prompt, **kwargs):
        assert "one open ready issue in owner/repo" in prompt
        if revoked:
            grant.unlink()
        output = {"status": "success", "summary": "Checked.", "details": {"issue_update": update}}
        yield "<task-output>" + json.dumps(output) + "</task-output>"

    calls = []
    def apply(repo, request):
        calls.append((repo, request))
        return "Comment present."

    monkeypatch.setattr(app.ai_backend, "stream", stream)
    monkeypatch.setattr(issues, "apply_update", apply)
    task = task_config.TaskConfig(id="ada", skill="engineer", description="", connection=conn)
    status, output, error, _ = asyncio.run(app.run_task(task))
    if revoked:
        assert status == "error" and "No current standing approval" in error
        assert calls == []
    else:
        assert status == "success" and not error
        assert calls == [("owner/repo", update)]
        assert "Comment present." in output["summary"]
