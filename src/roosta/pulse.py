"""Vault Pulse - live workspace telemetry for the dashboard."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKIP_PARTS = {
    ".git", ".obsidian", "node_modules", ".venv", "cache", ".roosta", ".cursor",
    # The gitignored private tree holds secrets, Workday reviews and the Teams
    # backups (teams-archive, teams-digest) - none of it belongs on the Pulse.
    "private",
}

# A project claiming to be live while nothing in its folder has changed for this
# long is worth a second look - either it restarts or its status is wrong.
STALE_AFTER_DAYS = 30
LIVE_STATUSES = {"active", "blocked"}

RELATED_SECTION = re.compile(r"^##\s+Related\s*$(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)
WIKILINK = re.compile(r"\[\[([^\]|#]+)")
README_LINK = re.compile(r"\]\(\s*(?:\.\./|/)?(?:projects/)?([a-z0-9][a-z0-9-]*)/README\.md")


def markdown_title(content: str, stem: str) -> str:
    """Resolve a display title from frontmatter, first heading, or filename."""
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            for line in content[3:end].splitlines():
                if line.strip().startswith("title:"):
                    title = line.split(":", 1)[1].strip().strip("\"'")
                    if title:
                        return title
    heading = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    if heading:
        return heading.group(1).strip()
    return stem.replace("-", " ").title()


def frontmatter_field(content: str, field: str) -> str:
    """Read one scalar value from a Markdown file's YAML frontmatter."""
    if not content.startswith("---"):
        return ""
    end = content.find("\n---", 3)
    if end == -1:
        return ""
    for line in content[3:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == field:
            return value.strip().strip("\"'")
    return ""


def frontmatter_text(content: str) -> str:
    """Flatten a Markdown file's YAML frontmatter values into searchable text."""
    if not content.startswith("---"):
        return ""
    end = content.find("\n---", 3)
    if end == -1:
        return ""
    values = []
    for line in content[3:end].splitlines():
        _, sep, value = line.partition(":")
        text = value if sep else line
        values.append(re.sub(r"[\[\]\"']", " ", text).strip(" -"))
    return " ".join(v for v in values if v)


def related_projects(content: str, known: set[str]) -> list[str]:
    """Project slugs linked from a README's `## Related` section."""
    section = RELATED_SECTION.search(content)
    if not section:
        return []
    body = section.group(1)
    found: list[str] = []
    for raw in WIKILINK.findall(body) + README_LINK.findall(body):
        slug = raw.strip().strip("/").split("/")[-1].removesuffix(".md")
        if slug in known and slug not in found:
            found.append(slug)
    return found


PROJECT_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
REVIEW_STATUSES = ("active", "blocked", "paused", "completed")
REVIEWER = "human:user"  # default; callers pass the profile's reviewer
VERIFIED_ENTRY = re.compile(r"by:\s*([^,}]+?)\s*,\s*at:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})")
JIRA_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-[0-9]{1,6})\b")
URL = re.compile(r"https?://[^\s)\]>\"'|]+")
LINK_KINDS = (
    ("atlassian.net/browse", "Jira"),
    ("atlassian.net/wiki", "Confluence"),
    ("confluence.", "Confluence"),
    ("docs.google.com", "Google Doc"),
    ("github", "GitHub"),
    ("teams.microsoft.com", "Teams"),
    ("miro.com", "Miro"),
)


def _frontmatter_lines(content: str) -> tuple[list[str], int] | None:
    """Split a document into lines and return the index of the closing `---`."""
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines, i
    return None


def provenance(content: str, today: str) -> dict:
    """Read `verified` / `stale_after` and say whether the doc is due a check."""
    entries: list[dict] = []
    split = _frontmatter_lines(content)
    if split:
        lines, end = split
        for i in range(1, end):
            if lines[i].split(":", 1)[0].strip() != "verified" or lines[i][:1].isspace():
                continue
            for j in range(i, end):
                if j > i and lines[j][:1] not in (" ", "\t", "-"):
                    break
                entries += [{"by": by, "at": at} for by, at in VERIFIED_ENTRY.findall(lines[j])]
            break
    status = frontmatter_field(content, "status") or "unknown"
    stale_after = frontmatter_field(content, "stale_after")[:10]
    human = any(entry["by"].startswith("human:") for entry in entries)
    expired = bool(stale_after) and stale_after <= today
    return {
        "verified": entries,
        "stale_after": stale_after,
        "state": "human" if human else "machine" if entries else "unverified",
        # Completed projects are reference material: they never fall due.
        "due": status != "completed" and (not entries or expired),
        "expired": expired,
    }


def markdown_section(content: str, heading: str) -> str:
    """Body of one `## heading` section, or an empty string."""
    match = re.search(
        rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", content,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def artefact_links(content: str, limit: int = 20, per_kind: int = 6) -> list[dict]:
    """Jira keys and external URLs a README points at, labelled by kind."""
    found: dict[str, dict] = {}
    for raw in URL.findall(content):
        url = raw.rstrip(".,;:")
        kind = next((label for needle, label in LINK_KINDS if needle in url), "Link")
        key = JIRA_KEY.search(url) if kind == "Jira" else None
        label = key.group(1) if key else re.sub(r"^https?://(www\.)?", "", url)[:60]
        found.setdefault(url, {"kind": kind, "label": label, "url": url})
    linked_keys = {link["label"] for link in found.values() if link["kind"] == "Jira"}
    for key in JIRA_KEY.findall(content):
        if key in linked_keys or key.startswith(("ISO-", "UTF-", "SHA-")):
            continue
        linked_keys.add(key)
        url = f"https://deept.atlassian.net/browse/{key}"
        found.setdefault(url, {"kind": "Jira", "label": key, "url": url})
    order = {label: i for i, (_, label) in enumerate(LINK_KINDS)}
    # First mentions win within a kind, so a ticket-heavy README still shows its docs.
    kept: list[dict] = []
    for link in found.values():
        if sum(1 for other in kept if other["kind"] == link["kind"]) < per_kind:
            kept.append(link)
    return sorted(kept, key=lambda link: order.get(link["kind"], 99))[:limit]


def _project_readme(root: Path, slug: str) -> Path:
    """Resolve `projects/<slug>/README.md` without permitting vault escape."""
    if not PROJECT_SLUG.fullmatch(slug or ""):
        raise ValueError("invalid project")
    projects = (root / "projects").resolve()
    readme = (projects / slug / "README.md").resolve()
    if readme.parent.parent != projects:
        raise ValueError("invalid project")
    if not readme.is_file():
        raise FileNotFoundError(slug)
    return readme


def project_brief(root: Path, slug: str, now: datetime | None = None) -> dict:
    """Everything the constellation drawer shows about one project."""
    now = now or datetime.now(timezone.utc)
    readme = _project_readme(root.resolve(), slug)
    content = readme.read_text(encoding="utf-8", errors="ignore")
    files = []
    for path in readme.parent.rglob("*"):
        if path.is_file() and not any(p.startswith(".") for p in path.relative_to(readme.parent).parts):
            files.append((path.stat().st_mtime, path))
    files.sort(reverse=True)
    vault = root.resolve()
    overview = markdown_section(content, "Overview")
    return {
        "name": slug,
        "title": markdown_title(content, slug),
        "status": frontmatter_field(content, "status") or "unknown",
        "last_updated": frontmatter_field(content, "last_updated"),
        "overview": overview.split("\n\n")[0][:900],
        "current_state": markdown_section(content, "Current state")[:12000],
        "provenance": provenance(content, now.date().isoformat()),
        "links": artefact_links(content),
        "recent_files": [
            {
                "path": path.relative_to(vault).as_posix(),
                "name": path.relative_to(readme.parent).as_posix(),
                "mtime": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
            }
            for mtime, path in files[:8]
        ],
    }


def _set_key(lines: list[str], end: int, key: str, value: str | None) -> int:
    """Set, add or (value None) drop one top-level scalar; returns the new end index."""
    for i in range(1, end):
        if not lines[i][:1].isspace() and lines[i].split(":", 1)[0].strip() == key:
            if value is None:
                del lines[i]
                return end - 1
            lines[i] = f"{key}: {value}"
            return end
    if value is None:
        return end
    lines.insert(end, f"{key}: {value}")
    return end + 1


def review_project(root: Path, slug: str, action: str, status: str = "",
                   now: datetime | None = None, reviewer: str = REVIEWER) -> dict:
    """Apply one weekly-review decision to a project README's frontmatter.

    `status` changes the lifecycle state (an edit, so `last_updated` moves);
    `confirm` records the user's word that the README is right (a verification, so
    only `verified` and `stale_after` move). Nothing below the frontmatter changes.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    readme = _project_readme(root.resolve(), slug)
    content = readme.read_text(encoding="utf-8")
    split = _frontmatter_lines(content)
    if not split:
        raise ValueError("README has no frontmatter")
    lines, end = split
    current = frontmatter_field(content, "status") or "unknown"
    if action == "status":
        if status not in REVIEW_STATUSES:
            raise ValueError("invalid status")
        end = _set_key(lines, end, "status", status)
        end = _set_key(lines, end, "last_updated", today)
        if status == "completed":
            end = _set_key(lines, end, "stale_after", None)
    elif action == "confirm":
        entry = f"  - {{ by: {reviewer}, at: {today} }}"
        at = next((i for i in range(1, end) if not lines[i][:1].isspace()
                   and lines[i].split(":", 1)[0].strip() == "verified"), None)
        if at is None:
            lines.insert(end, "verified:")
            at, end = end, end + 1
        elif lines[at].split(":", 1)[1].strip() not in ("", "[]"):
            raise ValueError("verified is not a block list")
        lines[at] = "verified:"
        if lines[at + 1].strip() != entry.strip():
            lines.insert(at + 1, entry)
            end += 1
        days = 30 if current == "active" else 90
        end = _set_key(lines, end, "stale_after",
                       None if current == "completed" else (now + timedelta(days=days)).date().isoformat())
    else:
        raise ValueError("invalid action")
    updated = "\n".join(lines)
    if updated != content:
        temp = readme.with_name(f".{readme.name}.review")
        temp.write_text(updated, encoding="utf-8")
        temp.replace(readme)
    return project_brief(root, slug, now)


# Layers beyond projects. Logs are hubs that would link everything, and
# calibration, promo, feedback and personal-growth pages stay off a shared map.
ENTITY_EXCLUDE = re.compile(r"(log|calibration|promo|feedback|personal|growth|review)", re.IGNORECASE)
PROJECT_PATH = re.compile(r"projects/([a-z0-9]+(?:-[a-z0-9]+)*)/")
PAGES_PER_PROJECT = 40


def entity_kind(rel: Path) -> str | None:
    """`person`, `incident` or `note` for a vault page that gets its own star."""
    parts = rel.parts
    if rel.name == "README.md" or ENTITY_EXCLUDE.search(rel.stem):
        return None
    if len(parts) == 3 and parts[:2] == ("notes", "people"):
        return "person"
    if len(parts) == 2 and parts[0] == "incidents":
        return "incident"
    if len(parts) == 2 and parts[0] == "notes":
        return "note"
    return None


def frontmatter_list(content: str, field: str) -> list[str]:
    """Values of a frontmatter list written inline (`[a, b]`) or as a block."""
    split = _frontmatter_lines(content)
    if not split:
        return []
    lines, end = split
    for i in range(1, end):
        key, sep, value = lines[i].partition(":")
        if not sep or lines[i][:1].isspace() or key.strip() != field:
            continue
        if value.strip():
            return [v.strip().strip("\"'") for v in value.strip().strip("[]").split(",") if v.strip()]
        values = []
        for line in lines[i + 1:end]:
            if not line.strip().startswith("- "):
                break
            values.append(line.strip()[2:].strip().strip("\"'"))
        return values
    return []


def name_slug(name: str) -> str:
    """`Chris Moore` -> `chris-moore`, matching person dossier filenames."""
    return re.sub(r"[^a-z0-9]+", "-", re.sub(r"\([^)]*\)", "", name.lower())).strip("-")


def link_entities(pages: dict[str, dict], readmes: dict[str, str], known: set[str]) -> list[dict]:
    """Attach project links to people, incidents and notes.

    A link counts from either side: the page names the project (wikilink or a
    `projects/<slug>/` path), the project README wikilinks the page, or - for a
    person - the README lists them under `people` or `owner`.
    """
    by_stem = {Path(path).stem: path for path in pages}
    by_person = {Path(path).stem: path for path, page in pages.items() if page["kind"] == "person"}
    for page in pages.values():
        text = page.pop("text")
        found = {raw.strip().split("/")[-1] for raw in WIKILINK.findall(text)} & known
        found |= set(PROJECT_PATH.findall(text)) & known
        found |= set(README_LINK.findall(text)) & known
        page["related"] = found
    for slug, readme in readmes.items():
        for raw in WIKILINK.findall(readme):
            target = by_stem.get(raw.strip().split("/")[-1].removesuffix(".md"))
            if target:
                pages[target]["related"].add(slug)
        for person in frontmatter_list(readme, "people") + [frontmatter_field(readme, "owner")]:
            target = by_person.get(name_slug(person)) if person else None
            if target:
                pages[target]["related"].add(slug)
    for page in pages.values():
        page["related"] = sorted(page["related"])
    return sorted(pages.values(), key=lambda page: (page["kind"], page["name"]))


def link_projects(signals: dict[str, dict], readmes: dict[str, str]) -> None:
    """Attach the documented relationship graph to each project signal."""
    names = set(signals)
    for name, signal in signals.items():
        signal["related"] = related_projects(readmes.get(name, ""), names - {name})
    # `## Related` is meant to be bidirectional; draw a half-written pair anyway.
    for name, signal in signals.items():
        for peer in signal["related"]:
            if name not in signals[peer]["related"]:
                signals[peer]["related"].append(name)
    for signal in signals.values():
        signal["related"].sort()


def scan_vault(root: Path) -> dict:
    """Count vault artefacts without touching secrets or dashboard internals."""
    root = root.resolve()
    projects_dir = root / "projects"
    project_names: list[str] = []
    if projects_dir.is_dir():
        project_names = sorted(
            p.name for p in projects_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )
    project_signals = {
        name: {
            "name": name,
            "title": name.replace("-", " ").title(),
            "status": "unknown",
            "owner": "",
            "keywords": "",
            "related": [],
            "recent_edits": 0,
            "last_activity": None,
            "age_days": None,
            "stale": False,
            "provenance": "unverified",
            "verification_due": False,
            "pages": [],
        }
        for name in project_names
    }
    project_readmes: dict[str, str] = {}
    entity_pages: dict[str, dict] = {}

    md_files = 0
    words = 0
    recent_edits: list[dict] = []
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    for path in root.rglob("*.md"):
        rel = path.relative_to(root)
        if any(part in SKIP_PARTS or part.startswith(".") for part in rel.parts):
            continue
        md_files += 1
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
            words += len(re.findall(r"\b\w+\b", text))
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if len(rel.parts) >= 3 and rel.parts[0] == "projects":
                project = project_signals.get(rel.parts[1])
                if project is not None:
                    if rel.parts[2:] == ("README.md",):
                        project["title"] = markdown_title(text, rel.parts[1])
                        project["status"] = frontmatter_field(text, "status") or "unknown"
                        project["owner"] = frontmatter_field(text, "owner")
                        project["keywords"] = frontmatter_text(text)
                        check = provenance(text, now.date().isoformat())
                        project["provenance"] = check["state"]
                        project["verification_due"] = check["due"]
                        project_readmes[rel.parts[1]] = text
                    last_activity = project["last_activity"]
                    if last_activity is None or mtime.isoformat() > last_activity:
                        project["last_activity"] = mtime.isoformat()
                    if mtime >= week_ago:
                        project["recent_edits"] += 1
                    if rel.parts[2:] != ("README.md",):
                        project["pages"].append({
                            "path": rel.as_posix(),
                            "title": markdown_title(text, path.stem),
                            "mtime": mtime.isoformat(),
                        })
            kind = entity_kind(rel)
            if kind:
                entity_pages[rel.as_posix()] = {
                    "name": rel.as_posix(),
                    "kind": kind,
                    "title": markdown_title(text, path.stem),
                    "keywords": frontmatter_text(text),
                    "last_activity": mtime.isoformat(),
                    "age_days": round(max(0.0, (now - mtime).total_seconds() / 86400), 2),
                    "text": text,
                }
            if mtime >= week_ago:
                recent_edits.append({
                    "path": rel.as_posix(),
                    "title": markdown_title(text, path.stem),
                    "mtime": mtime.isoformat(),
                })
        except OSError:
            continue

    link_projects(project_signals, project_readmes)
    entities = link_entities(entity_pages, project_readmes, set(project_signals))
    for signal in project_signals.values():
        signal["pages"] = sorted(signal["pages"], key=lambda page: page["mtime"], reverse=True)[:PAGES_PER_PROJECT]
    for signal in project_signals.values():
        if signal["last_activity"]:
            age = (now - datetime.fromisoformat(signal["last_activity"])).total_seconds()
            signal["age_days"] = round(max(0.0, age / 86400), 2)
        signal["stale"] = bool(
            signal["age_days"] is not None
            and signal["age_days"] >= STALE_AFTER_DAYS
            and signal["status"] in LIVE_STATUSES
        )

    skill_names = set()
    for skills_dir in (root / ".agents" / "skills", root / ".claude" / "skills"):
        if skills_dir.is_dir():
            skill_names.update(
                p.name for p in skills_dir.iterdir()
                if p.is_dir() and (p / "SKILL.md").is_file()
            )
    skills = len(skill_names)

    recent_edits.sort(key=lambda item: item["mtime"], reverse=True)
    return {
        "projects": len(project_names),
        "project_names": project_names[:48],
        "project_signals": list(project_signals.values()),
        "entities": entities,
        "stale_projects": sum(1 for s in project_signals.values() if s["stale"]),
        "stale_after_days": STALE_AFTER_DAYS,
        "markdown_files": md_files,
        "words": words,
        "skills": skills,
        "recent_files_7d": len(recent_edits),
        "hot_files": recent_edits[:6],
    }


def activity_stats(entries: list[dict], now: datetime | None = None) -> dict:
    """Summarise AI activity from the activity log."""
    now = now or datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    today = [entry for entry in entries if entry.get("started", "") >= today_start]
    runs = [entry for entry in today if entry.get("kind") == "skill"]
    running = [entry for entry in entries if entry.get("status") == "running"]
    return {
        "today_runs": len(runs),
        "today_success": sum(1 for entry in today if entry.get("status") == "success"),
        "today_errors": sum(1 for entry in today if entry.get("status") == "error"),
        "running_now": len(running),
        "today_ai_seconds": round(
            sum(entry.get("duration", 0) for entry in runs), 1
        ),
    }


def board_stats(
    handled: dict,
    board_active: int,
    board_later: int,
    health: dict,
) -> dict:
    """Summarise Assistant board throughput."""
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    cleared_today = sum(
        1 for value in handled.values()
        if isinstance(value, dict) and value.get("at", "") >= today_start
    )
    return {
        "board_active": board_active,
        "board_later": board_later,
        "cleared_today": cleared_today,
        "sensors_ok": bool(health.get("ok", True)),
        "degraded_sensors": len(health.get("degraded", [])),
    }


def command_mood(vault: dict, activity: dict, board: dict) -> dict:
    """Pick a playful operational headline from live telemetry."""
    if not board["sensors_ok"]:
        return {
            "code": "sensor_fog",
            "headline": "Sensor fog",
            "detail": (
                f"{board['degraded_sensors']} sensor"
                f"{'s' if board['degraded_sensors'] != 1 else ''} offline. "
                "The all-clear may be a mirage."
            ),
            "accent": "#d29922",
        }
    if activity["running_now"]:
        return {
            "code": "agents_active",
            "headline": "Agents active",
            "detail": (
                f"{activity['running_now']} skill"
                f"{'s' if activity['running_now'] != 1 else ''} running right now. "
                "The command centre hums."
            ),
            "accent": "#58a6ff",
        }
    if board["board_active"] == 0 and board["board_later"] == 0:
        return {
            "code": "all_clear",
            "headline": "All clear",
            "detail": "Nothing on the board. Either you're winning or it's suspiciously quiet.",
            "accent": "#3fb950",
        }
    if board["cleared_today"] >= 3:
        return {
            "code": "productive_orbit",
            "headline": "Productive orbit",
            "detail": (
                f"{board['cleared_today']} items cleared today. "
                "Momentum looks real."
            ),
            "accent": "#79c0ff",
        }
    if activity["today_errors"]:
        return {
            "code": "turbulence",
            "headline": "Turbulence detected",
            "detail": (
                f"{activity['today_errors']} failed pass"
                f"{'es' if activity['today_errors'] != 1 else ''} today. "
                "Worth a glance at the runner."
            ),
            "accent": "#f85149",
        }
    if vault["recent_files_7d"] >= 15:
        return {
            "code": "vault_alive",
            "headline": "Vault alive",
            "detail": (
                f"{vault['recent_files_7d']} files touched this week across "
                f"{vault['projects']} projects."
            ),
            "accent": "#d2a8ff",
        }
    return {
        "code": "steady_state",
        "headline": "Steady state",
        "detail": (
            f"{board['board_active']} on the board, "
            f"{vault['words']:,} words in the vault. Systems nominal."
        ),
        "accent": "#8b949e",
    }


def running_workloads(
    task_states: list[dict],
    running_activity: list[dict],
    chat_items: list[dict],
) -> list[dict]:
    """Normalise in-flight scheduled tasks, AI skill runs and board chats."""
    items: list[dict] = []
    covered_tasks: set[str] = set()

    for entry in running_activity:
        if entry.get("status") != "running":
            continue
        label = str(entry.get("label", "")).strip() or "unknown"
        covered_tasks.add(label)
        items.append({
            "kind": str(entry.get("kind", "skill")),
            "name": label,
            "detail": str(entry.get("source", "")).strip() or "scheduled",
            "status": "running",
            "started": entry.get("started", ""),
            "action": "trace",
            "trace_key": label,
        })

    for state in task_states:
        task_id = str(state.get("id", "")).strip()
        skill = str(state.get("skill", task_id)).strip()
        queued = bool(state.get("queued"))
        running = state.get("status") == "running"
        if not running and not queued:
            continue
        if running and (task_id in covered_tasks or skill in covered_tasks):
            continue
        covered_tasks.add(task_id or skill)
        items.append({
            "kind": "task",
            "name": task_id or skill,
            "detail": skill,
            "status": "queued" if queued and not running else "running",
            "started": state.get("last_run", "") if running else "",
            "action": "trace",
            "trace_key": task_id or skill,
        })

    for chat in chat_items:
        item_id = str(chat.get("id", "")).strip()
        title = str(chat.get("title", "")).strip() or "Conversation"
        item = {
            "kind": "chat",
            "name": title,
            "detail": "Assistant reply in progress",
            "status": "running",
            "started": "",
            "action": "open",
            "href": f"/assistant?item={item_id}" if item_id else "/assistant",
        }
        project_ref = str(chat.get("project_ref", "")).strip()
        if re.fullmatch(r"projects/[a-z0-9]+(?:-[a-z0-9]+)*", project_ref):
            item["project_ref"] = project_ref
        items.append(item)

    return items


def build_pulse(
    root: Path,
    activity_entries: list[dict],
    handled: dict,
    board_active: int,
    board_later: int,
    health: dict,
    task_states: list[dict] | None = None,
    running_activity: list[dict] | None = None,
    chat_items: list[dict] | None = None,
    now: datetime | None = None,
    active_project_refs: list[str] | None = None,
    active_project_items: list[dict] | None = None,
) -> dict:
    """Assemble the full pulse payload.

    `active_project_items` (`{id, title, project_ref}`) lets the constellation
    list each board item by name; bare refs still count when titles are unknown.
    """
    now = now or datetime.now(timezone.utc)
    vault = scan_vault(root)
    items = list(active_project_items or []) + [
        {"project_ref": ref} for ref in active_project_refs or []
    ]
    project_items: dict[str, list[dict]] = {}
    for item in items:
        project_items.setdefault(item.get("project_ref", ""), []).append(item)
    for project in vault["project_signals"]:
        linked = project_items.get(f"projects/{project['name']}", [])
        project["active_board_items"] = len(linked)
        project["board_items"] = [
            {"id": str(item["id"]), "title": str(item.get("title") or "Board item")}
            for item in linked if item.get("id")
        ]
    activity = activity_stats(activity_entries, now)
    board = board_stats(handled, board_active, board_later, health)
    running = running_workloads(
        task_states or [],
        running_activity or [],
        chat_items or [],
    )
    mood = command_mood(vault, activity, board)
    return {
        "generated_at": now.isoformat(),
        "local_hour": now.astimezone().hour,
        "vault": vault,
        "activity": activity,
        "board": board,
        "running": running,
        "mood": mood,
    }
