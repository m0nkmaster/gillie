"""Privacy-preserving product usage signals for dashboard evolution."""

from __future__ import annotations

import argparse
import json
import threading
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from roosta import paths


ALLOWED_EVENTS = {
    "assistant_opened",
    "item_opened",
    "chat_sent",
    "item_closed",
    "item_snoozed",
    "item_reopened",
    "item_unsnoozed",
    "item_pinned",
    "order_changed",
    "chat_started",
    "chat_archived",
    "chat_tab_opened",
    "idea_used",
}
ALLOWED_PROPERTIES = {
    "action_type": {"chat", "ticket", "teams", "jira", "email", "standup", "meeting", "document",
                    "review", "decision", "task"},
    "scope": {"general", "item", "person", "document", "worker", "hire", "inbox", "chats"},
    "mode": {"message", "recommendation"},
    "state": {"on", "off"},
    "order": {"recommended", "importance", "date", "type"},
    "preset": {"one_hour", "tomorrow", "next_monday"},
    "source": {"workspace", "starter"},
}


def normalise_event(body: object) -> tuple[str, dict[str, str]]:
    """Allow only coarse interaction metadata, never titles, ids or message text."""
    if not isinstance(body, dict):
        raise ValueError("invalid request")
    event = str(body.get("event", "")).strip()
    if event not in ALLOWED_EVENTS:
        raise ValueError("unknown event")
    raw = body.get("properties", {})
    if not isinstance(raw, dict):
        raise ValueError("invalid properties")
    properties: dict[str, str] = {}
    for key, value in raw.items():
        if key not in ALLOWED_PROPERTIES:
            raise ValueError("unknown property")
        text = str(value).strip()
        if text not in ALLOWED_PROPERTIES[key]:
            raise ValueError("invalid property value")
        properties[key] = text
    return event, properties


class UsageStore:
    """Append bounded local interaction signals and produce compact summaries."""

    def __init__(self, path: Path, max_entries: int = 5000) -> None:
        self.path = Path(path)
        self.max_entries = max_entries
        self._lock = threading.Lock()

    def record(self, body: object, now: datetime | None = None) -> dict:
        event, properties = normalise_event(body)
        instant = now or datetime.now(timezone.utc)
        row = {
            "at": instant.astimezone(timezone.utc).isoformat(),
            "event": event,
            "properties": properties,
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, separators=(",", ":")) + "\n")
            self._trim_if_needed()
        return row

    def _trim_if_needed(self) -> None:
        if not self.path.is_file() or self.path.stat().st_size < 1_000_000:
            return
        lines = self.path.read_text(encoding="utf-8").splitlines()
        if len(lines) > self.max_entries:
            self.path.write_text(
                "\n".join(lines[-self.max_entries:]) + "\n",
                encoding="utf-8",
            )

    def rows(self) -> list[dict]:
        if not self.path.is_file():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def summary(self, days: int = 14, now: datetime | None = None) -> dict:
        current = now or datetime.now(timezone.utc)
        since = current - timedelta(days=max(1, min(days, 90)))
        counts: Counter[str] = Counter()
        breakdowns: dict[str, Counter[str]] = defaultdict(Counter)
        recent = []
        for row in self.rows():
            try:
                instant = datetime.fromisoformat(str(row.get("at", "")))
            except ValueError:
                continue
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
            if instant < since:
                continue
            event = str(row.get("event", ""))
            if event not in ALLOWED_EVENTS:
                continue
            counts[event] += 1
            properties = row.get("properties", {})
            safe_properties = {}
            if isinstance(properties, dict):
                for key, value in properties.items():
                    if key in ALLOWED_PROPERTIES and str(value) in ALLOWED_PROPERTIES[key]:
                        text = str(value)
                        safe_properties[key] = text
                        breakdowns[f"{event}.{key}"][text] += 1
            recent.append({
                "at": instant.astimezone(timezone.utc).isoformat(),
                "event": event,
                "properties": safe_properties,
            })
        return {
            "since": since.astimezone(timezone.utc).isoformat(),
            "events": sum(counts.values()),
            "counts": dict(counts.most_common()),
            "breakdowns": {
                key: dict(values.most_common()) for key, values in sorted(breakdowns.items())
            },
            "recent": recent[-20:],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarise local Assistant usage")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument(
        "--path",
        type=Path,
        default=paths.CACHE_DIR / "product-usage.jsonl",
    )
    args = parser.parse_args()
    print(json.dumps(UsageStore(args.path).summary(args.days), indent=2))


if __name__ == "__main__":
    main()
