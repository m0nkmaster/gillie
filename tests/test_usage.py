import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from roosta import app as appmod
from roosta.usage import UsageStore, normalise_event


NOW = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)


def test_usage_rejects_content_and_unknown_metadata():
    assert normalise_event({
        "event": "item_opened",
        "properties": {"action_type": "jira", "scope": "item"},
    }) == ("item_opened", {"action_type": "jira", "scope": "item"})

    with pytest.raises(ValueError):
        normalise_event({"event": "item_opened", "properties": {"item_id": "PROJ-1"}})
    with pytest.raises(ValueError):
        normalise_event({"event": "chat_sent", "properties": {"message": "secret"}})
    with pytest.raises(ValueError):
        normalise_event({"event": "unknown", "properties": {}})


def test_usage_summary_counts_only_recent_valid_signals(tmp_path):
    store = UsageStore(tmp_path / "usage.jsonl")
    store.record(
        {"event": "item_opened", "properties": {"action_type": "jira", "scope": "item"}},
        NOW,
    )
    store.record(
        {"event": "chat_sent", "properties": {"scope": "item", "mode": "message"}},
        NOW,
    )
    old = {
        "at": (NOW - timedelta(days=30)).isoformat(),
        "event": "assistant_opened",
        "properties": {},
    }
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(old) + "\n")
        handle.write("not-json\n")

    summary = store.summary(days=14, now=NOW)

    assert summary["events"] == 2
    assert summary["counts"] == {"item_opened": 1, "chat_sent": 1}
    assert summary["breakdowns"]["item_opened.action_type"] == {"jira": 1}
    assert [row["event"] for row in summary["recent"]] == ["item_opened", "chat_sent"]


def test_usage_store_trims_large_history(tmp_path):
    store = UsageStore(tmp_path / "usage.jsonl", max_entries=2)
    store.path.write_text(
        "\n".join(
            json.dumps({
                "at": NOW.isoformat(),
                "event": "assistant_opened",
                "properties": {},
                "padding": "x" * 400_000,
            })
            for _ in range(3)
        ) + "\n",
        encoding="utf-8",
    )

    store.record({"event": "assistant_opened", "properties": {}}, NOW)

    assert len(store.path.read_text(encoding="utf-8").splitlines()) == 2


def test_usage_endpoint_records_only_valid_coarse_events(monkeypatch, tmp_path):
    store = UsageStore(tmp_path / "usage.jsonl")
    monkeypatch.setattr(appmod, "product_usage", store)

    response = asyncio.run(appmod.api_usage({
        "event": "chat_sent",
        "properties": {"scope": "general", "mode": "message"},
    }))
    rejected = asyncio.run(appmod.api_usage({
        "event": "chat_sent",
        "properties": {"message": "do not store this"},
    }))

    assert response.status_code == 200
    assert rejected.status_code == 400
    assert store.rows()[0]["properties"] == {"scope": "general", "mode": "message"}
