import json
import sys
from pathlib import Path

import pytest

from roosta import app as appmod
from roosta.cursor_sessions import read_cursor_transcript, valid_session_id


def test_valid_session_id_accepts_cursor_chat_uuid():
    assert valid_session_id("019bf2f4-9c27-49a5-8c09-707860e58585")
    assert not valid_session_id("../etc/passwd")
    assert not valid_session_id("")


def test_read_cursor_transcript_parses_user_and_assistant_turns(tmp_path, monkeypatch):
    session_id = "019bf2f4-9c27-49a5-8c09-707860e58585"
    root = tmp_path / "agent-transcripts" / session_id
    root.mkdir(parents=True)
    lines = [
        {"role": "user", "message": {"content": [
            {"type": "text", "text": "<user_query>Run the gardener</user_query>"},
        ]}},
        {"role": "assistant", "message": {"content": [
            {"type": "text", "text": "Done."},
        ]}},
    ]
    (root / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "roosta.cursor_sessions.cursor_transcript_dir",
        lambda workspace=None: tmp_path / "agent-transcripts",
    )

    turns = read_cursor_transcript(session_id)
    assert turns == [
        {"role": "user", "text": "Run the gardener"},
        {"role": "assistant", "text": "Done."},
    ]


def test_activity_transcript_endpoint_returns_turns(tmp_path, monkeypatch):
    async def scenario():
        from httpx import ASGITransport, AsyncClient

        session_id = "019bf2f4-9c27-49a5-8c09-707860e58585"
        eid = appmod.activity.start("pass", "skill", "manual")
        appmod.activity.set_session(eid, session_id)

        monkeypatch.setattr(
            appmod,
            "read_cursor_transcript",
            lambda sid: [{"role": "assistant", "text": "hello"}] if sid == session_id else [],
        )

        transport = ASGITransport(app=appmod.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/activity/{eid}/transcript")
            assert response.status_code == 200
            body = response.json()
            assert body["session_id"] == session_id
            assert body["turns"] == [{"role": "assistant", "text": "hello"}]
            assert f"agent --resume {session_id}" in body["resume_command"]

    import asyncio
    asyncio.run(scenario())


def test_read_claude_transcript_keeps_prompt_and_replies_only(tmp_path):
    from roosta.claude_code import read_claude_transcript

    session_id = "70734d05-4cb8-41bc-9e2a-712d13d75a9a"
    project = tmp_path / "-Users-sam-workspace"
    project.mkdir()
    lines = [
        {"type": "user", "message": {"role": "user", "content": "Run the assistant skill"}},
        {"type": "user", "isMeta": True, "message": {"content": [{"type": "text", "text": "meta"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}},
        {"type": "attachment"},
    ]
    (project / f"{session_id}.jsonl").write_text("\n".join(json.dumps(line) for line in lines))

    assert read_claude_transcript(session_id, tmp_path) == [
        {"role": "user", "text": "Run the assistant skill"},
        {"role": "assistant", "text": "Done."},
    ]
    assert read_claude_transcript("../etc/passwd", tmp_path) == []


def test_claude_activity_transcript_returns_outcome_and_claude_resume(monkeypatch):
    async def scenario():
        from httpx import ASGITransport, AsyncClient

        session_id = "70734d05-4cb8-41bc-9e2a-712d13d75a9a"
        eid = appmod.activity.start("skill", "assistant", "scheduled", backend="claude")
        appmod.activity.set_session(eid, session_id)
        appmod.activity.finish(eid, "success")
        final = '<task-output>{"summary": "4 items raised", "status": "success"}</task-output>'
        monkeypatch.setattr(
            appmod, "read_claude_transcript",
            lambda sid: [{"role": "assistant", "text": final}] if sid == session_id else [],
        )

        transport = ASGITransport(app=appmod.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            body = (await client.get(f"/api/activity/{eid}/transcript")).json()
        assert body["outcome"] == "4 items raised"
        assert f"claude --resume {session_id}" in body["resume_command"]
        assert "CURSOR_CONFIG_DIR" not in body["resume_command"]

    import asyncio
    asyncio.run(scenario())


def test_activity_static_routes_precede_param_route():
    """`/api/activity/stream` must register before `{activity_id}` or SSE 404s."""
    activity_paths = [
        getattr(route, "path", "")
        for route in appmod.app.router.routes
        if getattr(route, "path", "").startswith("/api/activity")
    ]
    assert activity_paths.index("/api/activity/stream") < activity_paths.index(
        "/api/activity/{activity_id}"
    )


def test_activity_rows_from_tests_stay_out_of_the_live_feed(isolated_activity_log):
    live = appmod.DASHBOARD_ROOT / "cache" / "activity.jsonl"
    before = live.stat().st_size if live.exists() else 0

    eid = appmod.activity.start("skill", "isolation-check", "manual")
    appmod.activity.finish(eid)

    assert "isolation-check" in isolated_activity_log.read_text()
    after = live.stat().st_size if live.exists() else 0
    assert after == before or "isolation-check" not in live.read_text()[before:]
