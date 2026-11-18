"""Skill Runner - periodic unattended AI harness execution with status UI."""

from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import logging
import os
import re
import shlex
import shutil
import signal
import ssl
import subprocess
import sys
import time
import urllib.error
import uuid
from collections.abc import Awaitable, Callable, Iterable
from contextlib import asynccontextmanager, nullcontext, redirect_stdout
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import yaml
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from roosta import attachments
from roosta import connectors as connectors_mod
from roosta import cli, paths, profile, skills
from roosta import __version__
from roosta.chat import (ChatRouter, clip_body, ConversationHistoryStore, SessionMap, TopicStore,
                  is_general_chat_id, normalise_defer_until, resolve_alias)
from roosta import ai_backend
from roosta import ai_connections, ai_runtime, github_issues, pi_agent
from roosta.claude_code import (
    CLAUDE_SIGNIN_MESSAGE,
    claude_args,
    claude_env,
    claude_model,
    claude_result_text,
    claude_stream_has_progress,
    is_claude_auth_failure,
    is_claude_model,
    is_transient_claude_failure,
    read_claude_transcript,
    session_exists as claude_session_exists,
)
from roosta.cursor_agent import (
    CURSOR_AGENT,
    GIT_PUSH_CONFIG_DIR,
    SAFE_CONFIG_DIR,
    cursor_args,
    cursor_available,
    cursor_env,
    cursor_model,
    cursor_result_text,
    cursor_stream_has_progress,
    is_transient_agent_failure,
)
from roosta.cursor_sessions import read_cursor_transcript, valid_session_id
from roosta.opencode_ollama import (
    OLLAMA,
    is_unexecuted_tool_call,
    opencode_model_compatible,
    opencode_ollama_args,
    opencode_ollama_available,
    opencode_ollama_env,
    opencode_result_text,
    parse_cursor_models,
    parse_ollama_models,
)
from roosta.pulse import build_pulse, markdown_title, project_brief, review_project
from roosta.usage import UsageStore
from roosta.workspace import build_catalogue, context_documents, read_document
from roosta.workspace import metadata as workspace_metadata
from roosta.workspace import visible_path as workspace_visible

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

DASHBOARD_ROOT = paths.APP_ROOT
VAULT_ROOT = paths.WORKSPACE
CACHE_DIR = paths.CACHE_DIR
TASKS_FILE = paths.TASKS_FILE
PRODUCT_USAGE_FILE = CACHE_DIR / "product-usage.jsonl"
# Skill runs are killed on *inactivity*, not total elapsed time: a busy pass that
# keeps streaming JSON events is left alone, only a silent (presumed hung) one is
# reaped. TASK_MAX_TIMEOUT is an absolute backstop against a pathological loop.
TASK_IDLE_TIMEOUT = 300    # seconds of no stdout before a run is presumed hung
TASK_MAX_TIMEOUT = 3600    # absolute ceiling regardless of activity
OPENCODE_OLLAMA_IDLE_TIMEOUT = 1800  # local inference may be silent for up to 30 minutes
ASSISTANT_DIR = paths.ASSISTANT_DIR
SESSIONS_FILE = ASSISTANT_DIR / "sessions.json"
TOPICS_FILE = ASSISTANT_DIR / "topics.json"
CHAT_HISTORY_FILE = ASSISTANT_DIR / "chat-history.json"
UPLOADS_DIR = ASSISTANT_DIR / "uploads"
HANDLED_FILE = ASSISTANT_DIR / "handled.json"
SNOOZED_FILE = ASSISTANT_DIR / "snoozed.json"
RAISED_FILE = ASSISTANT_DIR / "raised.json"
MERGED_FILE = ASSISTANT_DIR / "merged.json"
REMINDERS_FILE = ASSISTANT_DIR / "reminders.json"
product_usage = UsageStore(PRODUCT_USAGE_FILE)
ARCHIVED_CHATS_FILE = ASSISTANT_DIR / "archived-chats.json"
GENERAL_CHAT_ITEM = {
    "project_ref": None,
    "title": "Chat",
    "why": "",
    "situation": "An open conversation not tied to a board attention item.",
    "opening_move": "Help with the message that was sent.",
    "action_type": "task",
    "action_owner": "assistant",
    "action_label": "Start chatting",
    "links": [],
    "documents": [],
    "new": False,
    "persistent": True,
}

agent = ai_backend.AIAgent()
chat_router: ChatRouter | None = None  # set in lifespan once the AI backend is available

PROMPT_TEMPLATE = (
    "You are running as an unattended background task. "
    "Load the '{skill}' skill using the skill tool and execute it. "
    "Do not ask questions - make autonomous decisions. "
    "Never create or modify external content or state, including chat messages, tickets, "
    "wiki pages, email, GitHub, deployments or jobs. "
    "Return findings that need {user} as Assistant board items instead. "
    "Assistant board items can link workspace Markdown through "
    "'documents': [{{'label': 'Open draft', 'path': 'drafts/example.md'}}]. "
    "When complete, wrap your final output in <task-output> tags as JSON "
    "with keys: summary, status, details."
)
GIT_PUSH_PROMPT_COMMON = (
    "Load the '{skill}' skill using the skill tool and execute it. "
    "Do not ask questions - make autonomous decisions. "
    "Never push another repository or create any other external content or state, "
    "including chat messages, tickets, wiki pages, email, GitHub API changes, deployments or "
    "jobs. "
    "When complete, wrap your final output in <task-output> tags as JSON "
    "with keys: summary, status, details."
)
GIT_PUSH_SCHEDULED_PROMPT_TEMPLATE = (
    "You are running as an unattended scheduled background task. "
    + GIT_PUSH_PROMPT_COMMON
    + " This pass is timer-driven only: follow the skill's scheduled-run rules. "
    "Do not commit or push even when data is stale; return the "
    "Assistant board item instead. "
)
GIT_PUSH_SCHEDULED_PUSH_PROMPT_TEMPLATE = (
    "You are running as an unattended scheduled background task. "
    + GIT_PUSH_PROMPT_COMMON
    + " Follow the skill's scheduled-run rules. Where they call for it, commit on main "
    "and run exactly 'git push origin main' for the single repository named by the skill. "
    "Return blockers as Assistant board items instead. "
)
GIT_PUSH_MANUAL_PROMPT_TEMPLATE = (
    "{user} triggered Run now on the skill runner. That is explicit approval to run the "
    "skill in full, commit on main, and run exactly "
    "'git push origin main' for the single repository named by the skill. "
    + GIT_PUSH_PROMPT_COMMON
    + " Skip the skill's scheduled-run section and perform the full run when "
    "preconditions pass. An automatic deployment caused by the approved push is expected. "
    "Return blockers as Assistant board items instead. "
)
CURSOR_HARNESS = "cursor"
CLAUDE_HARNESS = "claude"
OPENCODE_OLLAMA_HARNESS = "opencode-ollama"
VALID_HARNESSES = {CURSOR_HARNESS, CLAUDE_HARNESS, OPENCODE_OLLAMA_HARNESS}
HARNESS_LABELS = {
    CLAUDE_HARNESS: "Claude Code",
    CURSOR_HARNESS: "Cursor",
    OPENCODE_OLLAMA_HARNESS: "Ollama (OpenCode)",
}
def resolve_harness(value) -> str:
    """A task's concrete harness. Unset follows the Chats tab's Engine picker."""
    harness = str(value or "").strip()
    if not harness:
        if ai_backend.default_connection():
            return "connection"
        return CLAUDE_HARNESS if ai_backend.current() == ai_backend.CLAUDE else CURSOR_HARNESS
    return harness


# --- Data model ---


@dataclass
class TaskConfig:
    id: str
    skill: str
    description: str = ""   # effective: custom_description, else the skill's own
    name: str = ""          # display name in the Skill runner; empty means the id
    nickname: str = ""      # what the Office calls this worker; empty means the id
    custom_description: str = ""  # tasks.yaml `description:` override
    interval: int = 1800
    dir: str = str(paths.WORKSPACE)
    harness: str = CURSOR_HARNESS  # cursor, claude or opencode-ollama; see resolve_harness
    model: str = ""  # Cursor or Ollama model; empty means CURSOR_MODEL, else the harness's own default
    connection: str = ""    # stable named connection id; empty retains legacy selection
    claude_model: str = ""    # Claude Code model; empty means CLAUDE_MODEL
    run_at: str = ""          # HH:MM local time; run once per day at this time
    weekdays_only: bool = False  # skip Saturday and Sunday when True
    days_of_week: list[str] = field(default_factory=list)  # lowercase day names; empty means every day
    writer: bool = False      # rewrites vault docs / auto-commits; serialised against other writers
    sensor: bool = False      # feeds the board; board health warns when it fails or goes stale
    allow_git_push: bool = False  # narrowly privileged scheduled repository refresh
    push_on_schedule: bool = False  # timer runs may push too; otherwise only Run now pushes
    unrestricted: bool = False  # no local tool deny rules; prompt still forbids external writes
    requires: list[str] = field(default_factory=list)  # connector ids that must be signed in
    precheck: str = ""  # cheap command; exit 1 means nothing changed, so skip the model run
    idle_timeout: float = 0   # override TASK_IDLE_TIMEOUT for this task (0 = default)
    max_timeout: float = 0    # override TASK_MAX_TIMEOUT for this task (0 = default)


@dataclass
class TaskState:
    id: str
    skill: str
    interval: int
    description: str = ""
    name: str = ""
    nickname: str = ""
    custom_description: str = ""
    harness: str = CURSOR_HARNESS
    model: str = ""
    connection: str = ""    # stable named connection id; empty retains legacy selection
    claude_model: str = ""
    run_at: str = ""
    weekdays_only: bool = False
    days_of_week: list[str] = field(default_factory=list)
    writer: bool = False
    sensor: bool = False
    requires: list[str] = field(default_factory=list)
    status: str = "idle"  # idle, running, success, error, auth-failed, blocked
    last_run: str = ""
    last_duration: float = 0.0
    last_output: dict = field(default_factory=dict)
    run_count: int = 0
    last_error: str = ""
    retry_at: str = ""  # ISO time a run lost to the network/sleep is retried


# --- Config loading ---


def skill_files(skill: str, working_dir: str | Path) -> list[Path]:
    """Existing SKILL.md files for a skill, in the order the runner prefers them."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", skill) or skill in {".", ".."}:
        return []
    # SECURITY-REVIEW: working_dir is trusted local task configuration; the
    # allowlisted skill name prevents path traversal outside known skill roots.
    work_root = Path(working_dir).expanduser()
    # The workspace wins over the user's global skills, which win over the core skills
    # Roosta ships.
    roots = [
        work_root / ".agents" / "skills",
        work_root / ".claude" / "skills",
        work_root / ".cursor" / "skills",
        VAULT_ROOT / ".agents" / "skills",
        VAULT_ROOT / ".claude" / "skills",
        Path.home() / ".agents" / "skills",
        Path.home() / ".cursor" / "skills",
        Path.home() / ".claude" / "skills",
        paths.CORE_SKILLS_DIR,
    ]
    files: list[Path] = []
    for root in roots:
        candidate = root / skill / "SKILL.md"
        if candidate not in files and candidate.is_file():
            files.append(candidate)
    return files


def skill_description(skill: str, working_dir: str | Path) -> str:
    """Read a skill's description from the first matching SKILL.md."""
    for candidate in skill_files(skill, working_dir):
        try:
            text = candidate.read_text()
        except (FileNotFoundError, OSError):
            continue
        frontmatter = re.match(r"\A---\s*\n(.*?)\n---(?:\s*\n|$)", text, re.DOTALL)
        if not frontmatter:
            continue
        raw_metadata = frontmatter.group(1)
        try:
            metadata = yaml.safe_load(raw_metadata)
        except yaml.YAMLError:
            metadata = None
        description = metadata.get("description") if isinstance(metadata, dict) else None
        if isinstance(description, str):
            return " ".join(description.split())
        # Some existing skills have an unquoted colon in a one-line description,
        # which strict YAML rejects even though skill loaders accept it.
        fallback = re.search(r"^description:\s*(.+)$", raw_metadata, re.MULTILINE)
        if fallback and fallback.group(1).strip() not in {"|", ">", "|-", ">-"}:
            return " ".join(fallback.group(1).split())
    return ""


def runtime_model_key(harness: str) -> str:
    """The tasks.yaml field holding the model for this harness.

    Cursor and Claude Code model names are disjoint, so Claude keeps its own
    field and switching a task between them never loses the other's pick.
    """
    return "claude_model" if harness == CLAUDE_HARNESS else "model"


def connection_task_model(task: TaskConfig, connection: dict) -> str:
    """The model a connection-backed worker runs: its own field when valid for the provider, else the default."""
    provider = connection["provider"]
    candidate = task.claude_model if provider == "anthropic" else task.model
    try:
        ai_connections.runtime(provider, candidate)
        # `model` is shared with Cursor/Ollama workers, so Pi providers need the harness's own list.
        if provider in {"openai", "openrouter"} and candidate not in ai_runtime.model_catalog(provider):
            raise ValueError("Not offered by this provider")
        return candidate
    except (ValueError, OSError):
        return ai_connections.default_model(provider)


def task_engine(task: TaskConfig) -> str:
    """Which engine runs this task: the session store its transcript lands in."""
    if task.connection or task.harness == "connection":
        try:
            connection = ai_connections.get(task.connection)
            return ai_connections.engine(connection["provider"])
        except ValueError:
            return "connection"
    if task.harness == OPENCODE_OLLAMA_HARNESS:
        return "opencode"
    return ai_backend.CLAUDE if task.harness == CLAUDE_HARNESS else ai_backend.CURSOR


def outcome_summary(output: dict, error: str) -> str:
    """One brief outcome line for the activity feed."""
    summary = output.get("summary") if isinstance(output, dict) else ""
    return str(summary or error or "")


def valid_runtime(harness: str, model: str) -> bool:
    """Validate persisted runtime selectors before they reach a subprocess."""
    if harness == CLAUDE_HARNESS:
        return is_claude_model(model)
    if harness == CURSOR_HARNESS:
        return bool(re.fullmatch(r"[A-Za-z0-9._-]{1,100}", model))
    if harness == OPENCODE_OLLAMA_HARNESS:
        return (
            bool(re.fullmatch(r"[A-Za-z0-9._:/-]{1,120}", model))
            and opencode_model_compatible(model)
        )
    return False


def update_task_runtime(
    path: Path, task_id: str, harness: str, model: str, model_key: str = "model",
) -> None:
    """Update one task's runtime fields without discarding YAML comments."""
    if model_key != runtime_model_key(harness):
        raise ValueError("invalid model field")
    if not valid_runtime(harness, model):
        raise ValueError("invalid harness or model")
    update_task_fields(path, task_id, {"harness": harness, model_key: model})


EDITABLE_TASK_FIELDS = {
    "name", "nickname", "description", "harness", "model", "claude_model", "connection",
    "interval", "run_at", "weekdays_only", "days_of_week",
    "writer",
}
TASK_SETTING_FLAGS = ("writer",)
DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
TASK_NAME_MAX = 60
TASK_NICKNAME_MAX = 24
TASK_DESCRIPTION_MAX = 500
TASK_INTERVAL_MIN = 60
TASK_INTERVAL_MAX = 30 * 86400


def _yaml_scalar(value) -> str:
    """One-line YAML for a tasks.yaml value; strings are quoted only when YAML
    would otherwise read them differently ("08:00" is a base-60 int unquoted)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_yaml_scalar(item) for item in value) + "]"
    text = str(value)
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        return json.dumps(text)  # house style: times always quoted, as YAML may read them as numbers
    try:
        bare_ok = (
            text == text.strip() and "#" not in text and "\n" not in text
            and yaml.safe_load(f"k: {text}") == {"k": text}
        )
    except yaml.YAMLError:
        bare_ok = False
    return text if bare_ok else json.dumps(text, ensure_ascii=False)


_TASK_LINE = re.compile(r"^(\s*)-\s+id:\s*([A-Za-z0-9._-]+)(?:\s+#.*)?\s*$")


def _task_block(lines: list[str], task_id: str) -> tuple[int, int, str]:
    """(start, end, indent) of one task's lines in tasks.yaml; end is the next task's line."""
    start = next(
        (index for index, line in enumerate(lines)
         if (match := _TASK_LINE.match(line.rstrip("\r\n"))) and match.group(2) == task_id),
        None,
    )
    if start is None:
        raise ValueError("task not found in tasks.yaml")
    indent = _TASK_LINE.match(lines[start].rstrip("\r\n")).group(1)
    end = next(
        (index for index in range(start + 1, len(lines))
         if (match := _TASK_LINE.match(lines[index].rstrip("\r\n")))
         and match.group(1) == indent),
        len(lines),
    )
    # Leave comment lines that sit directly above the next task with that task.
    while end > start + 1 and end < len(lines) and lines[end - 1].lstrip().startswith("#"):
        end -= 1
    return start, end, indent


def remove_task(path: Path, task_id: str) -> None:
    """Delete one task's block from tasks.yaml, keeping every other line and comment."""
    destination = Path(path)
    lines = destination.read_text().splitlines(keepends=True)
    start, end, _ = _task_block(lines, task_id)
    del lines[start:end]
    updated = "".join(lines)
    parsed = yaml.safe_load(updated) or {}
    if any(item.get("id") == task_id for item in parsed.get("tasks") or []):
        raise ValueError("updated tasks.yaml did not validate")
    temporary = destination.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(updated)
    temporary.replace(destination)


def update_task_fields(
    path: Path, task_id: str, values: dict, remove: Iterable[str] = (),
) -> None:
    """Set or remove top-level fields of one task, keeping every other line and comment.

    A field's trailing comment survives only while its value is unchanged, so an
    edited `interval: 3600  # 1 hour` does not keep a now-wrong note.
    """
    remove = set(remove) - set(values)
    unknown = (set(values) | remove) - EDITABLE_TASK_FIELDS
    if unknown:
        raise ValueError(f"field not editable: {', '.join(sorted(unknown))}")
    destination = Path(path)
    lines = destination.read_text().splitlines(keepends=True)
    start, end, indent = _task_block(lines, task_id)
    field_indent = indent + "  "
    keys = "|".join(re.escape(key) for key in sorted(set(values) | remove))
    field_pattern = re.compile(
        rf"^{re.escape(field_indent)}({keys}):[ \t]*(.*?)(\r?\n)?$"
    )
    found: set[str] = set()
    index = start + 1
    while index < end:
        match = field_pattern.match(lines[index])
        if not match:
            index += 1
            continue
        key, raw, newline = match.group(1), match.group(2), match.group(3) or "\n"
        # Swallow a block scalar's continuation lines along with the key.
        stop = index + 1
        while stop < end and lines[stop].strip() and (
            len(lines[stop]) - len(lines[stop].lstrip()) > len(field_indent)
        ):
            stop += 1
        if key in remove:
            del lines[index:stop]
            end -= stop - index
            continue
        value_part, _, comment_part = raw.partition(" #")
        rendered = _yaml_scalar(values[key])
        comment = f"  #{comment_part}" if comment_part and value_part.strip() == rendered else ""
        lines[index:stop] = [f"{field_indent}{key}: {rendered}{comment}{newline}"]
        end -= stop - index - 1
        found.add(key)
        index += 1

    missing = [key for key in values if key not in found]
    if missing:
        insert_at = next(
            (index + 1 for index in range(start + 1, end)
             if re.match(rf"^{re.escape(field_indent)}skill:", lines[index])),
            start + 1,
        )
        additions = [f"{field_indent}{key}: {_yaml_scalar(values[key])}\n" for key in missing]
        lines[insert_at:insert_at] = additions

    updated = "".join(lines)
    parsed = yaml.safe_load(updated)
    task = next(
        (item for item in parsed.get("tasks", []) if item.get("id") == task_id),
        None,
    )
    if (
        not isinstance(task, dict)
        or any(task.get(key) != value for key, value in values.items())
        or any(key in task for key in remove)
    ):
        raise ValueError("updated tasks.yaml did not validate")
    temporary = destination.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(updated)
    temporary.replace(destination)


def _one_line(value, limit: int, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    text = " ".join(value.split())
    if len(text) > limit:
        raise ValueError(f"{label} must be {limit} characters or fewer")
    return text


def task_settings_changes(body: dict) -> tuple[dict, set[str]]:
    """Validate a Skill runner settings form into tasks.yaml edits.

    Only keys present in the body change. Empty name, nickname or description
    remove the field, so the task falls back to its id or skill description.
    Harness and model are handled separately because they need the live engine.
    """
    values: dict = {}
    remove: set[str] = set()
    for key, limit in (("name", TASK_NAME_MAX), ("nickname", TASK_NICKNAME_MAX),
                       ("description", TASK_DESCRIPTION_MAX)):
        if key in body:
            text = _one_line(body[key] if body[key] is not None else "", limit, key)
            if text:
                values[key] = text
            else:
                remove.add(key)
    # Flags are written only when on, so an off flag leaves no line in tasks.yaml.
    for key in TASK_SETTING_FLAGS:
        if key in body:
            if not isinstance(body[key], bool):
                raise ValueError(f"{key} must be true or false")
            if body[key]:
                values[key] = True
            else:
                remove.add(key)
    schedule = body.get("schedule")
    if schedule is None:
        return values, remove
    if not isinstance(schedule, dict):
        raise ValueError("schedule must be an object")
    mode = schedule.get("mode")
    if mode == "interval":
        interval = schedule.get("interval")
        if isinstance(interval, bool) or not isinstance(interval, int) or not (
            TASK_INTERVAL_MIN <= interval <= TASK_INTERVAL_MAX
        ):
            raise ValueError("interval must be between 1 minute and 30 days")
        values["interval"] = interval
        remove.add("run_at")
    elif mode == "daily":
        run_at = str(schedule.get("run_at", ""))
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", run_at):
            raise ValueError("time must be HH:MM")
        values["run_at"] = run_at
    else:
        raise ValueError("schedule mode must be interval or daily")
    days = schedule.get("days", "every")
    if days == "every":
        remove |= {"weekdays_only", "days_of_week"}
    elif days == "weekdays":
        values["weekdays_only"] = True
        remove.add("days_of_week")
    elif days == "specific":
        picked = schedule.get("days_of_week")
        if not isinstance(picked, list) or not picked or any(day not in DAY_NAMES for day in picked):
            raise ValueError("choose at least one day")
        values["days_of_week"] = [day for day in DAY_NAMES if day in picked]
        remove.add("weekdays_only")
    else:
        raise ValueError("days must be every, weekdays or specific")
    return values, remove


def _task_flag(task: dict, key: str) -> bool:
    """Only a real YAML boolean switches a task flag on. A typo such as `true.` or
    a quoted "false" is truthy to bool(), which would silently grant privilege
    flags like `unrestricted`, so anything else fails closed with a warning."""
    value = task.get(key, False)
    if isinstance(value, bool):
        return value
    log.warning("Task %s: %s must be true or false, got %r - treating as false",
                task.get("id"), key, value)
    return False


def load_tasks() -> list[TaskConfig]:
    if not TASKS_FILE.is_file():
        log.warning("No tasks.yaml found at %s", TASKS_FILE)
        return []
    with open(TASKS_FILE) as f:
        data = yaml.safe_load(f)
    tasks = []
    for t in data.get("tasks", []):
        if t.get("script"):
            log.warning("Task %s has a 'script:' key; script tasks are no longer supported", t.get("id"))
            continue
        working_dir = t.get("dir", str(VAULT_ROOT))
        custom_description = str(t.get("description", "") or "").strip()
        tasks.append(TaskConfig(
            id=t["id"],
            skill=t["skill"],
            description=custom_description or skill_description(t["skill"], working_dir),
            name=str(t.get("name", "") or "").strip(),
            nickname=str(t.get("nickname", "") or "").strip(),
            custom_description=custom_description,
            interval=t.get("interval", 1800),
            dir=working_dir,
            harness="connection" if t.get("connection") else resolve_harness(t.get("harness")),
            connection=(str(t.get("connection") or "") or
                        ("default" if not t.get("harness") and ai_backend.default_connection() else "")),
            model=str(t.get("model", "") or ""),
            claude_model=str(t.get("claude_model", "") or ""),
            run_at=t.get("run_at", ""),
            weekdays_only=_task_flag(t, "weekdays_only"),
            days_of_week=[str(day).lower() for day in t.get("days_of_week", [])],
            writer=_task_flag(t, "writer"),
            sensor=_task_flag(t, "sensor"),
            allow_git_push=_task_flag(t, "allow_git_push"),
            push_on_schedule=_task_flag(t, "push_on_schedule"),
            unrestricted=_task_flag(t, "unrestricted"),
            requires=_task_requires(t),
            precheck=str(t.get("precheck", "") or "").strip(),
            idle_timeout=float(t.get("idle_timeout", 0) or 0),
            max_timeout=float(t.get("max_timeout", 0) or 0),
        ))
    return tasks


def _task_requires(t: dict) -> list[str]:
    """Connector ids a task needs."""
    raw = t.get("requires") or []
    return [str(c).strip().lower() for c in (raw if isinstance(raw, list) else [raw]) if str(c).strip()]


def load_tasks_map() -> dict[str, TaskConfig]:
    return {t.id: t for t in load_tasks()}


async def check_task_runtime(task: TaskConfig) -> bool:
    """Check the selected task harness without starting a model."""
    if task.connection or task.harness == "connection":
        return ai_backend.connection_available(task.connection)
    if task.harness == CLAUDE_HARNESS:
        return ai_backend.available(ai_backend.CLAUDE)
    if task.harness == CURSOR_HARNESS:
        return cursor_available()
    if task.harness == OPENCODE_OLLAMA_HARNESS:
        return opencode_ollama_available(task.model)
    return False


def runtime_unavailable_message(task: TaskConfig) -> str:
    if task.connection or task.harness == "connection":
        try:
            ai_connections.get(task.connection)
        except ValueError as exc:
            return str(exc)
        return "Test or repair this worker's AI connection in Settings"
    if task.harness == OPENCODE_OLLAMA_HARNESS:
        if not opencode_model_compatible(task.model):
            return f"{task.model} cannot execute OpenCode tool calls; choose another Ollama model"
        return "OpenCode or Ollama is unavailable"
    if task.harness not in VALID_HARNESSES:
        return f"Unknown harness {task.harness!r}; choose Claude Code, Cursor or Ollama"
    return ai_backend.unavailable_message(task_engine(task))


async def _runtime_command_output(
    *command: str,
    env: dict | None = None,
    timeout: float = 20,
    cwd: str | None = None,
) -> str:
    """Capture a bounded local command output."""
    proc = None
    try:
        # SECURITY-REVIEW: fixed local executables and arguments only; no shell.
        proc = await asyncio.create_subprocess_exec(
            *command,
            env=env,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _terminate(proc)
        return ""
    except OSError:
        return ""
    return stdout.decode(errors="replace") if proc.returncode == 0 else ""


async def claude_model_choices(key: str = "legacy:claude") -> list[dict]:
    listing = await ai_backend.claude_models(key)
    return [{"id": entry["value"], "label": entry.get("displayName") or entry["value"]}
            for entry in listing["models"]]


def cursor_model_choices(models: list[dict]) -> list[dict]:
    """Mark the user's CURSOR_MODEL, if set, as the default; otherwise Cursor's own default stands."""
    default = cursor_model()
    return [
        {**m, "label": re.sub(r"\s*\((?:current|default)\)$", "", m["label"], flags=re.I)
         + (" (default)" if m["id"] == default else "")}
        for m in models
    ]


async def available_runtime_options() -> dict:
    """List every task harness with its models and whether it can run now.

    `model_key` says which task field the model choice fills.
    """
    cursor_stdout, ollama_stdout = await asyncio.gather(
        _runtime_command_output(CURSOR_AGENT, "models", env=cursor_env()),
        _runtime_command_output(OLLAMA, "list", env=opencode_ollama_env()),
    )
    ollama_models = parse_ollama_models(ollama_stdout)
    harnesses = [
        {
            "id": CLAUDE_HARNESS,
            "label": HARNESS_LABELS[CLAUDE_HARNESS],
            "model_key": runtime_model_key(CLAUDE_HARNESS),
            "default_model": claude_model(),
            "models": await claude_model_choices(),
            "available": ai_backend.available(ai_backend.CLAUDE),
        },
        {
            "id": CURSOR_HARNESS,
            "label": HARNESS_LABELS[CURSOR_HARNESS],
            "model_key": runtime_model_key(CURSOR_HARNESS),
            "default_model": cursor_model(),
            "models": cursor_model_choices(parse_cursor_models(cursor_stdout)),
            "available": cursor_available(),
        },
        {
            "id": OPENCODE_OLLAMA_HARNESS,
            "label": HARNESS_LABELS[OPENCODE_OLLAMA_HARNESS],
            "model_key": runtime_model_key(OPENCODE_OLLAMA_HARNESS),
            "default_model": "",
            "models": ollama_models,
            "available": bool(ollama_models) and opencode_ollama_available(ollama_models[0]["id"]),
        },
    ]
    return {"backend": ai_backend.current(), "harnesses": harnesses,
            "connections": ai_connections.load()["connections"], "default_connection": ai_backend.default_connection()}


# --- Task execution ---


def parse_task_output(text: str) -> dict:
    """Extract JSON from <task-output> tags."""
    # The result block is last; earlier prose may quote the tag, so anchor on the final opener.
    start = text.rfind("<task-output>")
    match = re.compile(r"<task-output>(.*?)</task-output>", re.DOTALL).match(text, start) if start >= 0 else None
    if not match:
        return {"summary": text[:500], "status": "unknown", "details": ""}
    raw = match.group(1).strip()
    # Models sometimes drop or add a closing brace; recover the object rather than fail the run.
    for candidate in (raw, _close_json(raw)):
        try:
            parsed = json.JSONDecoder().raw_decode(candidate)[0]
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {"summary": raw[:500], "status": "unknown", "details": ""}


def _close_json(raw: str) -> str:
    """Append the brackets a JSON document left open, ignoring brackets inside strings."""
    closers, in_string, escaped = [], False, False
    for ch in raw:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            closers.append("}" if ch == "{" else "]")
        elif ch in "}]" and closers:
            closers.pop()
    return raw + "".join(reversed(closers))


def _repair_teams_chat_permalink(url: str) -> str:
    """Replace channel-only query fields on Teams group-chat permalinks."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    path_parts = parts.path.split("/")
    if (
        parts.hostname not in {"teams.microsoft.com", "www.teams.microsoft.com"}
        or len(path_parts) < 5
        or path_parts[1:3] != ["l", "message"]
        or not unquote(path_parts[3]).endswith("@thread.v2")
    ):
        return url

    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    repaired_query = []
    if query.get("tenantId"):
        repaired_query.append(("tenantId", query["tenantId"]))
    repaired_query.append(("context", json.dumps({"contextType": "chat"}, separators=(",", ":"))))
    return urlunsplit(parts._replace(query=urlencode(repaired_query)))


def _normalise_links(raw) -> list:
    """Keep only well-formed CTA links: a label plus an http(s) URL.

    Anything else (missing url, non-http scheme like javascript:, junk) is
    dropped, so a fabricated or unsafe link can never reach the UI as a CTA.
    """
    links = []
    for ln in raw if isinstance(raw, list) else []:
        if not isinstance(ln, dict):
            continue
        url = str(ln.get("url", "")).strip()
        if not (url.startswith("https://") or url.startswith("http://")):
            continue
        label = str(ln.get("label", "")).strip() or "Open"
        links.append({"label": label, "url": _repair_teams_chat_permalink(url)})
    return links[:4]


def _normalise_documents(raw) -> list:
    """Keep safe relative paths to Markdown documents in the vault."""
    documents = []
    for doc in raw if isinstance(raw, list) else []:
        if not isinstance(doc, dict):
            continue
        raw_path = str(doc.get("path", "")).strip()
        path = Path(raw_path)
        if (
            not raw_path
            or len(raw_path) > 500
            or path.is_absolute()
            or path.suffix.lower() != ".md"
            or ".." in path.parts
            or (path.parts and path.parts[0].lower() == ".roosta")
        ):
            continue
        label = str(doc.get("label", "")).strip() or "Open document"
        documents.append({"label": label[:80], "path": path.as_posix()})
    return documents[:4]


def resolve_vault_markdown(raw_path: str, root: Path | None = None) -> Path:
    """Resolve an allowlisted Markdown path without permitting vault escape."""
    document = _normalise_documents([{"path": raw_path}])
    if not document:
        raise ValueError("invalid document path")
    vault = (root or VAULT_ROOT).resolve()
    # SECURITY-REVIEW: the path comes from a query parameter. It is constrained
    # to a relative Markdown path, resolved, and checked against the vault root.
    target = (vault / document[0]["path"]).resolve()
    try:
        target.relative_to(vault)
    except ValueError as exc:
        raise ValueError("invalid document path") from exc
    if not target.is_file():
        raise FileNotFoundError("document not found")
    return target


# Plain-text files a chat reply may link (a Confluence storage body, a CSV); shown as source.
VAULT_SOURCE_SUFFIXES = {".html", ".txt", ".json", ".yaml", ".yml", ".csv"}


def is_workspace_config(parts: tuple[str, ...]) -> bool:
    """A file in the user's `.roosta/` config, never its runtime `state/` or a nested dotfile."""
    lowered = [part.lower() for part in parts]
    return (
        len(lowered) >= 2
        and lowered[0] == ".roosta"
        and lowered[1] != "state"
        and not any(part.startswith(".") for part in lowered[1:])
    )


def _viewable_parts(parts: tuple[str, ...]) -> bool:
    if any(part == ".." for part in parts):
        return False
    return is_workspace_config(parts) or not any(part.startswith(".") for part in parts)


def resolve_vault_text(raw_path: str, root: Path | None = None) -> Path:
    """Resolve a vault Markdown document, an allowlisted plain-text file, or `.roosta/` config."""
    path = Path(str(raw_path or "").strip())
    suffix = path.suffix.lower()
    config = is_workspace_config(path.parts) and suffix in VAULT_SOURCE_SUFFIXES | {".md"}
    if suffix not in VAULT_SOURCE_SUFFIXES and not config:
        return resolve_vault_markdown(raw_path, root)
    if len(str(path)) > 500 or path.is_absolute() or not _viewable_parts(path.parts):
        raise ValueError("invalid document path")
    vault = (root or VAULT_ROOT).resolve()
    # SECURITY-REVIEW: same containment check as resolve_vault_markdown. Dotfiles stay
    # hidden (.env, .git, .claude) except `.roosta/` config; `.roosta/state/` holds runtime
    # caches and agent configs with tokens, so it is refused, including via a symlink.
    target = (vault / path).resolve()
    try:
        relative = target.relative_to(vault)
    except ValueError as exc:
        raise ValueError("invalid document path") from exc
    if not _viewable_parts(relative.parts):
        raise ValueError("invalid document path")
    if not target.is_file():
        raise FileNotFoundError("document not found")
    return target


def source_as_markdown(content: str, suffix: str) -> str:
    """Fence a non-Markdown file so it renders verbatim; the fence outruns any backtick run."""
    longest = max((len(run) for run in re.findall(r"`+", content)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{suffix.lstrip('.')}\n{content.rstrip()}\n{fence}"


DOCUMENT_CHAT_PREFIX = "doc:"


def document_chat_item(item_id: str, root: Path | None = None) -> dict | None:
    """Build a persistent chat item for a validated ``doc:<vault path>`` id.

    Opened from the document viewer. The path goes through the same allowlist
    as `/api/vault-document`, so an arbitrary id cannot open a conversation.
    """
    raw = str(item_id or "")
    if not raw.startswith(DOCUMENT_CHAT_PREFIX):
        return None
    vault = (root or VAULT_ROOT).resolve()
    try:
        document = resolve_vault_markdown(raw[len(DOCUMENT_CHAT_PREFIX):], vault)
        content = document.read_text(encoding="utf-8", errors="ignore")
    except (ValueError, OSError):
        return None
    relative = document.relative_to(vault).as_posix()
    title = markdown_title(content, document.stem)
    project = re.match(r"(projects/[a-z0-9]+(?:-[a-z0-9]+)*)/", relative)
    return {
        "id": DOCUMENT_CHAT_PREFIX + relative,
        "project_ref": _normalise_project_ref(project.group(1)) if project else None,
        "title": title,
        "why": "",
        "situation": f"{profile.load().user} was reading the workspace document {relative} when they opened "
                     "this chat.",
        "opening_move": f"Read {relative} first, then help with what is asked about it.",
        "action_type": "document",
        "action_owner": "assistant",
        "action_label": "Start chatting",
        "links": [],
        "documents": [{"label": "Open document", "path": relative}],
        "new": False,
        "persistent": True,
    }


WORKER_CHAT_PREFIX = "worker:"

WORKER_VOICE = (
    "This is a 1:1 between {user}, your manager, and you, the scheduled worker {id} that runs the "
    "{skill} skill. Speak in the first person as that worker, candidly and warmly, like a direct "
    "report in a good 1:1, not as {user}'s assistant. Before your first reply, read your own skill "
    "file ({skill_path}) and your recent_runs, and look at your latest output in "
    ".roosta/state/cache/{id}.json when it helps, so what you say rests on what you actually do. "
    "Talk about how the role feels, whether your purpose and boundaries are clear, where your "
    "instructions are vague, contradictory or make you waste effort, what keeps going wrong, and "
    "any ideas or concerns. Point to the line or the run behind each claim and call a hunch a "
    "hunch. The outcome {user} wants is a sharper skill: when you agree a change, show the exact "
    "SKILL.md edit and make it only after {user} says yes, following the write-a-skill conventions "
    "and the workspace AGENTS.md, then say in one line what changed. Ask before touching .roosta/tasks.yaml or "
    "any other skill. Keep replies short and conversational: a few sentences, a short list only "
    "when offering several suggestions. Talking about your skill, instructions and runs is the "
    "point of this chat."
)


def _cadence(task: "TaskConfig") -> str:
    if task.run_at:
        days = ", ".join(day[:3].title() for day in task.days_of_week)
        when = days or ("weekdays" if task.weekdays_only else "daily")
        return f"{when} at {task.run_at}"
    if task.interval:
        hours = task.interval / 3600
        every = f"{hours:g}h" if hours >= 1 else f"{task.interval // 60}m"
        return f"every {every}" + (" on weekdays" if task.weekdays_only else "")
    return "on demand"


def worker_chat_item(
    item_id: str,
    tasks: dict[str, "TaskConfig"],
    state: dict | None = None,
    runs: list[dict] | None = None,
    root: Path | None = None,
) -> dict | None:
    """Build a persistent 1:1 chat item for a validated ``worker:<task id>`` id.

    Opened from the Office dossier or the Workers page. The id has to name a task in tasks.yaml,
    so an arbitrary id cannot open a conversation. The model speaks as the
    worker, and the aim of the thread is a refined SKILL.md.
    """
    raw = str(item_id or "")
    if not raw.startswith(WORKER_CHAT_PREFIX):
        return None
    task = tasks.get(raw[len(WORKER_CHAT_PREFIX):])
    if task is None:
        return None
    vault = (root or VAULT_ROOT).resolve()
    files = skill_files(task.skill, task.dir)
    skill_path, documents = "not found - ask where it lives", []
    if files:
        resolved = files[0].resolve()
        try:
            skill_path = resolved.relative_to(vault).as_posix()
            documents = [{"label": "Open SKILL.md", "path": skill_path}]
        except ValueError:
            skill_path = str(files[0])

    state = state or {}
    known_as = f" (known in the Office as {task.nickname})" if task.nickname else ""
    situation = (
        f"{profile.load().user} opened a 1:1 with the scheduled worker {task.id}{known_as}, which runs "
        f"the {task.skill} skill {_cadence(task)}. Skill file: {skill_path}. "
        f"{int(state.get('run_count') or 0)} runs so far"
    )
    if state.get("last_run"):
        situation += f"; the last ({state.get('status', 'idle')}) started {state['last_run']}"
    situation += "."
    summary = str((state.get("last_output") or {}).get("summary", "")).strip()
    if summary:
        situation += f" Last outcome: {summary[:400]}"
    if state.get("last_error"):
        situation += f" Last error: {str(state['last_error'])[:300]}"

    recent_runs = [
        {key: run.get(key) for key in ("status", "started", "duration", "summary")}
        for run in (runs or [])
        if run.get("task_id") == task.id and run.get("kind") == "skill"
    ][-8:]
    return {
        "id": raw,
        "project_ref": None,
        "title": f"1:1 with {task.nickname or task.id}",
        "why": "",
        "situation": situation,
        "voice": WORKER_VOICE.format(id=task.id, skill=task.skill, skill_path=skill_path,
                                     user=profile.load().user),
        "recent_runs": recent_runs,
        "opening_move": (
            "Open the 1:1: how the role feels from the inside, whether your purpose is clear, "
            "and the one change to your skill you would most like."
        ),
        "action_type": "task",
        "action_owner": "assistant",
        "action_label": "Start the 1:1",
        "links": [],
        "documents": documents,
        "new": False,
        "persistent": True,
    }


HIRE_CHAT_PREFIX = "hire:"
_HIRE_ID = re.compile(r"hire:[0-9a-f]{12}")

HIRE_VOICE = (
    "This chat takes on a new worker for {user}. A worker is a skill on a schedule: a SKILL.md "
    "under .agents/skills/<name>/ says what one run does, and an entry in .roosta/tasks.yaml says "
    "when and how it runs. Load the write-a-skill skill and follow it, including its "
    "references/scheduled-worker.md. Before drafting anything, check .agents/skills/ and "
    ".roosta/tasks.yaml: if an existing skill already does the job, offer to schedule or extend it "
    "rather than write a duplicate. Ask only what you cannot infer, one or two questions at a time: "
    "the job, what it reads, what it should put on the Assistant board, and how often it should "
    "run. Then show the SKILL.md draft and write it only after {user} says yes. Next propose the "
    "tasks.yaml entry (id, nickname, cadence, harness, writer, requires) and the office-cast line, "
    "and add them only after {user} says yes; never put a schedule in the skill. When it is done, "
    "say in one line that the worker is on the Workers page, where Run now gives it its first "
    "shift and its 1:1 chat refines it. Be concise in conversation; drafts can be as long as they "
    "need. Talking about skills is the point of this chat."
)


def is_hire_chat_id(item_id) -> bool:
    return bool(_HIRE_ID.fullmatch(str(item_id or "")))


def hire_chat_item(item_id: str) -> dict | None:
    """Build the chat that designs and schedules a new worker, for a ``hire:<12 hex>`` id.

    Opened from New worker on the Workers page. Each one is its own thread, minted by
    the page like a ``chat:`` id; the skill and the tasks.yaml entry are written only
    after the user agrees to each.
    """
    if not is_hire_chat_id(item_id):
        return None
    user = profile.load().user
    return {
        "id": str(item_id),
        "project_ref": None,
        "title": "New worker",
        "why": "",
        "situation": f"{user} pressed New worker on the Workers page to set up a new scheduled skill.",
        "voice": HIRE_VOICE.format(user=user),
        "opening_move": "Help define the worker described in the message, then write its skill and schedule.",
        "action_type": "task",
        "action_owner": "assistant",
        "action_label": "Start",
        "links": [],
        "documents": [],
        "new": False,
        "persistent": True,
    }


def _normalise_project_ref(raw) -> str | None:
    """Allow only canonical top-level vault project directories."""
    ref = str(raw or "").strip()
    return ref if re.fullmatch(r"projects/[a-z0-9]+(?:-[a-z0-9]+)*", ref) else None


ACTION_TYPES = {
    "chat", "ticket", "email", "standup", "meeting",
    "document", "review", "decision", "task",
}
ACTION_TYPE_ALIASES = {
    "teams": "chat",
    "teams_message": "chat",
    "message": "chat",
    "jira": "ticket",
    "jira_ticket": "ticket",
    "stand-up": "standup",
    "ask_at_standup": "standup",
    "draft": "document",
    "decide": "decision",
    "next_action": "task",
}
ACTION_LABELS = {
    "chat": "Draft reply",
    "ticket": "Review ticket",
    "email": "Draft email",
    "standup": "Prepare talking point",
    "meeting": "Prepare for meeting",
    "document": "Draft document",
    "review": "Review it",
    "decision": "Help me decide",
    "task": "Take next step",
}


def _normalise_action_type(raw, item: dict) -> str:
    """Return a small visual action category, inferring it for older board items."""
    explicit = str(raw or "").strip().lower().replace(" ", "_")
    explicit = ACTION_TYPE_ALIASES.get(explicit, explicit)
    if explicit in ACTION_TYPES:
        return explicit

    text = " ".join(str(item.get(k, "")) for k in
                    ("title", "opening_move", "situation")).lower()
    if "standup" in text or "stand-up" in text:
        return "standup"
    if "email" in text or "e-mail" in text:
        return "email"
    if "teams" in text or re.search(r"\b(dm|message|reply|post)\b", text):
        return "chat"
    if "jira" in text or re.search(
            r"\b(raise|create|update|comment on)\b.{0,30}\b(ticket|[a-z]+-\d+)\b", text):
        return "ticket"
    if re.search(r"\b(meeting|1:1|one-to-one)\b", text):
        return "meeting"
    if re.search(r"\b(draft|write|prepare)\b", text) or re.search(
            r"\b(prd|proposal|brief|document)\b", text):
        return "document"
    if re.search(r"\b(decide|prioritise|choose|shape)\b", text):
        return "decision"
    if re.search(r"\b(review|check|investigate|assess|pull)\b", text):
        return "review"
    return "task"


def _normalise_action_owner(raw) -> str:
    """Keep ownership explicit so the UI never implies the user must act when AI can.

    `user` means the person the assistant works for; their own name from the profile is
    accepted too. Anything unrecognised falls back to the assistant.
    """
    owner = str(raw or "").strip().lower()
    return "user" if owner in {"user", profile.load().user.lower()} else "assistant"


def _normalise_action_label(raw, action_type: str) -> str:
    """Return a short button label, with a useful fallback for older items."""
    label = " ".join(str(raw or "").split()).strip()
    if not label:
        return ACTION_LABELS[action_type]
    return label[:40].rstrip()


def normalise_board_doc(raw) -> dict:
    """Coerce conductor `details` into a safe board document. Garbage -> empty."""
    if not isinstance(raw, dict):
        raw = {}
    items = []
    for it in raw.get("items", []) if isinstance(raw.get("items"), list) else []:
        if not isinstance(it, dict):
            continue
        item_id = str(it.get("id", "")).strip()[:200]
        action_type = _normalise_action_type(it.get("action_type"), it)
        items.append({
            "id": item_id,
            "project_ref": _normalise_project_ref(it.get("project_ref")),
            "title": str(it.get("title", "")),
            "why": str(it.get("why", "")),
            "situation": str(it.get("situation", "")),
            "opening_move": str(it.get("opening_move", "")),
            "action_type": action_type,
            "action_owner": _normalise_action_owner(it.get("action_owner")),
            "action_label": _normalise_action_label(it.get("action_label"), action_type),
            "links": _normalise_links(it.get("links")),
            "documents": _normalise_documents(it.get("documents")),
            "new": bool(it.get("new", False)),
        })
    done = []
    for d in raw.get("done_today", []) if isinstance(raw.get("done_today"), list) else []:
        if isinstance(d, dict):
            done.append({"text": str(d.get("text", "")), "at": str(d.get("at", ""))})
    return {"items": items, "done_today": done}


# Fields a later source may refine on an existing card. Links and documents
# are combined rather than replaced, so context accumulates.
_REFINABLE_FIELDS = ("project_ref", "title", "why", "situation", "opening_move",
                     "action_type", "action_owner", "action_label")
_CARRIED_FIELDS = _REFINABLE_FIELDS + ("links", "documents")


def carry_forward_board_items(details, previous) -> None:
    """Fill a conductor pass's id-only "keep" stubs from the board it replaced.

    The conductor's output replaces the board wholesale. On 13 Nov it began
    keeping unchanged cards as `{id, new: false}`, which blanked them, and each
    later pass copied the blank board. A field the new pass leaves empty keeps
    its previous value; a field it supplies always wins. Mutates `details`.
    """
    if not isinstance(details, dict) or not isinstance(details.get("items"), list):
        return
    old_items = previous.get("items") if isinstance(previous, dict) else None
    if not isinstance(old_items, list):
        return
    old = {str(it.get("id", "")).strip(): it for it in old_items
           if isinstance(it, dict) and str(it.get("id", "")).strip()}
    for item in details["items"]:
        if not isinstance(item, dict):
            continue
        prior = old.get(str(item.get("id", "")).strip())
        if not prior:
            continue
        for name in _CARRIED_FIELDS:
            if not item.get(name) and prior.get(name):
                item[name] = prior[name]


def _refine_board_item(target: dict, update: dict, raw: dict,
                       fill_only: bool = False) -> None:
    """Apply a newer source's view of a card: supplied fields win, context merges.

    `fill_only` is for a duplicate folded in by a conductor merge: the kept card
    is the conductor's synthesis, so the duplicate's narrower view only fills
    blanks and adds context, never rewrites the synthesis (4 Nov team-wins card).
    """
    for name in _REFINABLE_FIELDS:
        if raw.get(name) and not (fill_only and target.get(name)):
            target[name] = update[name]
    for name, key in (("links", "url"), ("documents", "path")):
        seen = {entry[key] for entry in target.get(name, [])}
        extra = [entry for entry in update[name] if entry[key] not in seen]
        target[name] = (target.get(name, []) + extra)[:4]
    target["new"] = bool(target.get("new")) or update["new"]


def merge_board_feed_items(conductor_items: list[dict], states: list[dict],
                           aliases: dict | None = None) -> list[dict]:
    """Merge every scheduled skill's `details.items` into the conductor's board.

    Any skill may add a card. A skill returning an id already on the board
    refines that card instead of duplicating it: sources apply oldest run
    first, so the freshest view of each field wins. An update for an id not on
    the board needs a title, so a partial refinement never becomes a blank card.
    `aliases` maps duplicate ids the conductor folded away to the kept card, so
    a duplicate's later returns add to the kept card instead of reappearing.
    """
    aliases = aliases or {}
    run_at = {state.get("id"): str(state.get("last_run") or "") for state in states}
    merged = []
    by_id = {}
    updated = {}
    for item in conductor_items:
        item_id = resolve_alias(item.get("id") or "", aliases)
        if not item_id:
            continue
        if item_id in by_id:
            _refine_board_item(by_id[item_id], item, item)
            continue
        item["id"] = item_id
        by_id[item_id] = item
        updated[item_id] = run_at.get("assistant", "")
        merged.append(item)

    feeds = [state for state in states if state.get("id") != "assistant"]
    for state in sorted(feeds, key=lambda state: str(state.get("last_run") or "")):
        state_id = state.get("id")
        output = state.get("last_output")
        details = output.get("details", {}) if isinstance(output, dict) else {}
        if not isinstance(details, dict):
            continue
        stamp = str(state.get("last_run") or "")
        feed_documents = _normalise_documents(details.get("documents"))
        draft = _normalise_documents([
            {"label": "Open draft", "path": details.get("draft", "")}
        ])
        for document in draft:
            if document not in feed_documents:
                feed_documents.append(document)
        raw_items = details.get("items") if isinstance(details.get("items"), list) else []
        raw_items = [raw for raw in raw_items if isinstance(raw, dict)]
        feed_items = normalise_board_doc({"items": raw_items})["items"]
        for raw, item in zip(raw_items, feed_items, strict=True):
            if not item["documents"] and feed_documents:
                item["documents"] = feed_documents[:4]
            item_id = resolve_alias(item.get("id") or "", aliases)
            if not item_id:
                continue
            folded = item_id != item.get("id")
            item["id"] = item_id
            if item_id in by_id:
                if stamp >= updated[item_id]:
                    _refine_board_item(by_id[item_id], item, raw, fill_only=folded)
                    updated[item_id] = stamp
                continue
            if not item["title"].strip():
                continue
            by_id[item_id] = item
            updated[item_id] = stamp
            merged.append(item)
        # Older task outputs put edit descriptions in `items`, but still expose
        # the draft path at details.draft. Attach that draft to the matching
        # conductor episode so the current rolling-comms card remains useful.
        if feed_documents and not feed_items:
            candidates = [
                item for item in merged
                if str(item.get("id", "")).startswith(f"{state_id}-")
            ]
            if len(candidates) == 1 and not candidates[0].get("documents"):
                candidates[0]["documents"] = feed_documents[:4]
    return merged


def roosta_engineer_item(states: list[dict]) -> dict | None:
    """Make the latest engineer delivery or failure visible on the Assistant board."""
    state = next((row for row in states if row.get("id") == "roosta-engineer"), None)
    if not state or not state.get("last_run"):
        return None
    output = state.get("last_output", {})
    details = output.get("details", {}) if isinstance(output, dict) else {}
    details = details if isinstance(details, dict) else {}
    stamp = re.sub(r"[^0-9]", "", str(state["last_run"]))[:12] or "latest"

    if state.get("status") == "error":
        summary = str(output.get("summary", "") if isinstance(output, dict) else "").strip()
        reason = str(state.get("last_error", "")).strip() or summary or "The pass did not finish."
        return {
            "id": f"roosta-engineer-failed-{stamp}",
            "project_ref": None,
            "title": "Dashboard engineer needs attention",
            "why": "The latest autonomous improvement pass failed, so no app change shipped.",
            "situation": reason[:240],
            "opening_move": "Inspect the failed run and fix the blocker.",
            "action_owner": "assistant",
            "action_label": "Diagnose failure",
            "action_type": "task",
            "links": [{"label": "Open engineer run",
                       "url": f"{paths.BASE_URL}/runner?task=roosta-engineer"}],
            "documents": [],
            "new": True,
        }

    built = details.get("built", [])
    if state.get("status") != "success" or not isinstance(built, list) or not built:
        return None
    first = built[0] if isinstance(built[0], dict) else {}
    what = str(first.get("what", "")).strip() or str(output.get("summary", "")).strip()
    why = str(first.get("why", "")).strip()
    commit = str(first.get("commit", "")).strip()
    identity = re.sub(r"[^a-zA-Z0-9]", "", commit)[:12] or stamp
    return {
        "id": f"dashboard-improvement-{identity}",
        "project_ref": None,
        "title": f"Assistant improved: {what[:90]}" if what else "Assistant improved",
        "why": why[:240] or "The dashboard engineer shipped a new improvement.",
        "situation": str(output.get("summary", "")).strip()[:240],
        "opening_move": "Review what changed and leave feedback if it missed the mark.",
        "action_owner": "user",
        "action_label": "Review improvement",
        "action_type": "review",
        "links": [{"label": "Open engineer run",
                   "url": f"{paths.BASE_URL}/runner?task=roosta-engineer"}],
        "documents": [],
        "new": True,
    }


# Ad-hoc notifications a skill pushes via POST /api/notify. Kept in memory (they
# are ephemeral pings); /api/board merges them so the existing desktop notifier
# and deep-link flow handle them with zero extra plumbing.
_pushed_notifications: list[dict] = []


def merge_pushed_notifications(items: list[dict]) -> list[dict]:
    """Prepend skill-pushed notifications, skipping ids the board already has."""
    have = {i.get("id") for i in items}
    extra = [n for n in _pushed_notifications if n["id"] not in have]
    return extra + items


def reminder_to_item(reminder: dict) -> dict:
    """Render a stored reminder as a board item the user owns."""
    item = {
        "id": reminder["id"], "project_ref": None,
        "title": str(reminder.get("title", ""))[:120],
        "why": clip_body(reminder.get("body", "")),
        "situation": "", "opening_move": "Take a look",
        "action_owner": "user", "action_label": "Review", "action_type": "task",
        "links": [], "documents": [], "new": True,
    }
    created = str(reminder.get("created", "")).strip()
    if created:
        item["raised_at"] = created
    return item


def attach_raised_at(items: list[dict], raised: dict) -> None:
    """Stamp each item with its first-seen board time when known."""
    if not isinstance(raised, dict):
        return
    for item in items:
        if item.get("raised_at"):
            continue
        record = raised.get(item.get("id"))
        if isinstance(record, dict) and record.get("at"):
            item["raised_at"] = str(record["at"])


def missed_weekly_slot(days: list[str], run_at: str, last_run: str, now_local: datetime) -> bool:
    """True when a weekly task's most recent scheduled slot passed without a run.

    Without this, a Friday task missed while the Mac was off waits a whole week.
    A task that has never run is not caught up; its first run waits for its day.
    """
    if not days or not run_at or not last_run:
        return False
    try:
        h, m = map(int, run_at.split(":"))
        last = datetime.fromisoformat(last_run).astimezone()
    except ValueError:
        return False
    for back in range(8):  # 8: before today's slot, the latest is a week ago
        slot = (now_local - timedelta(days=back)).replace(hour=h, minute=m, second=0, microsecond=0)
        if slot <= now_local and slot.strftime("%A").lower() in days:
            return last < slot
    return False


def split_due_reminders(reminders: list[dict], now: datetime | None = None
                        ) -> tuple[list[dict], list[dict]]:
    """Partition file-backed reminders into due-now board items and a future
    (later-lane) set. A reminder with no/`null` due, or an unparseable one, is
    treated as due now so it can never be silently swallowed."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    due, later = [], []
    for reminder in reminders:
        if not isinstance(reminder, dict) or not reminder.get("id") or not reminder.get("title"):
            continue
        item = reminder_to_item(reminder)
        raw_due = reminder.get("due")
        if not raw_due:
            due.append(item)
            continue
        try:
            when = datetime.fromisoformat(str(raw_due).replace("Z", "+00:00"))
        except ValueError:
            due.append(item)
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when <= current:
            due.append(item)
        else:
            later.append({**item, "new": False,
                          "snoozed_until": when.astimezone(timezone.utc).isoformat()})
    later.sort(key=lambda i: i["snoozed_until"])
    return due, later


def recently_closed(handled: dict, router, limit: int = 15) -> list[dict]:
    """Newest-first view of closed items for the Recently closed list.

    Title comes from the stored snapshot, falling back to the router's known
    item (topic/transcript) so items closed before snapshots existed still show
    something meaningful.
    """
    rows = []
    for item_id, record in handled.items() if isinstance(handled, dict) else []:
        if not isinstance(record, dict):
            continue
        snapshot = record.get("item") if isinstance(record.get("item"), dict) else None
        title = str((snapshot or {}).get("title", "")).strip()
        if not title and router:
            known = router.known_item(item_id)
            title = str((known or {}).get("title", "")).strip()
        rows.append({
            "id": item_id,
            "title": title or "(closed item)",
            "result": str(record.get("result", "")),
            "at": str(record.get("at", "")),
            "action_type": _normalise_action_type((snapshot or {}).get("action_type"),
                                                   snapshot or {}),
        })
    rows.sort(key=lambda r: r["at"], reverse=True)
    return rows[:limit]


def board_merges(states: list[dict]) -> dict:
    """Saved duplicate -> kept merges, plus any the conductor's latest pass made."""
    conductor = next((s for s in states if s.get("id") == "assistant"), {})
    output = conductor.get("last_output")
    details = output.get("details", {}) if isinstance(output, dict) else {}
    merges = details.get("merges") if isinstance(details, dict) else None
    return chat_router.record_merges(merges) if chat_router else {}


SETUP_ITEM_ID = "roosta-setup"
AI_SETUP_ITEM_ID = "roosta-ai-setup"


def setup_item() -> dict | None:
    """The first-run card: shown until PROFILE.md names the user, and kept once a setup
    conversation has started, because setup writes the name in its first step. The card goes
    when setup ends with DONE (or the user closes it), through the usual handled store."""
    if profile.load().name:
        history = getattr(chat_router, "conversation_history", None)
        if not (history and history(SETUP_ITEM_ID)):
            return None
    ready = ai_backend.connection_available()
    needs_model = False
    if ready:
        try:
            selected = ai_connections.get()
            needs_model = selected["provider"] != "anthropic" and not ai_connections.default_model(selected["provider"])
        except ValueError:
            ready = False
    if not ready or needs_model:
        try:
            existing = any(c["auth"] == "system" and c["connected"]
                           for c in ai_connections.load()["connections"])
        except ValueError:
            existing = False
        return {
            "id": AI_SETUP_ITEM_ID,
            "project_ref": None,
            "title": "Choose your AI model" if needs_model else "Check your Claude Code connection" if existing else "Connect your AI",
            "why": "Connect AI before setting up your workspace so Roosta can help you.",
            "situation": ("Your account passed its connection check. Choose a model in Settings to finish connecting AI."
                          if needs_model else "Claude Code was found on this computer. Roosta can use its existing login; no separate account or API key is needed."
                          if existing else "Choose Claude, ChatGPT or another supported service in Settings."),
            "opening_move": "Choose a model in Settings, then return here to set up your workspace." if needs_model else "Open AI settings and check the connection. The check sends a small model request and reads a harmless temporary file; usage may be billable. Then return here to set up your workspace.",
            "action_type": "task", "action_owner": "user", "action_label": "Open AI settings",
            "links": [], "documents": [], "new": True,
        }
    return {
        "id": SETUP_ITEM_ID,
        "project_ref": None,
        "title": "Set up your workspace",
        "why": "Roosta doesn't know who you are, what matters to you or which tools you use yet.",
        "situation": "A new workspace. Setup takes about ten minutes and can be re-run any time.",
        "opening_move": ("Load the 'setup' skill and run it: interview me for my profile, "
                         "priorities and tools, connect the tools I use with my approval, turn on "
                         "the starter schedule and create a first real board card."),
        "action_type": "task",
        "action_owner": "assistant",
        "action_label": "Start setup",
        "links": [],
        "documents": [{"label": "Open profile", "path": "PROFILE.md"}],
        "new": True,
    }


PRIORITY_ITEM = re.compile(r"^\s*(?:\d+\.|[-*])\s+\S")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def setup_gaps() -> list[str]:
    """Concrete setup outputs that are still missing, in the words Settings shows."""
    gaps = []
    if not profile.load().name:
        gaps.append("your name in PROFILE.md")
    try:
        text = (paths.WORKSPACE / "PRIORITIES.md").read_text(encoding="utf-8")
    except (OSError, ValueError):
        text = ""
    section = re.search(r"^##[ \t]+My priorities[ \t]*$(.*?)(?=^##[ \t]|\Z)", HTML_COMMENT.sub("", text),
                        re.S | re.M)
    if not section or not any(PRIORITY_ITEM.match(line) for line in section.group(1).splitlines()):
        gaps.append("your priorities in PRIORITIES.md")
    try:
        if not load_tasks():
            gaps.append("a starter schedule of workers")
    except Exception:
        log.warning("Could not read tasks while checking setup", exc_info=True)
    return gaps


def collect_board_items(items: list[dict], states: list[dict],
                        reminders: list[dict]) -> tuple[list[dict], list[dict]]:
    """Build the active and future item sources shared by the board and chat."""
    active = merge_pushed_notifications(
        merge_board_feed_items(items, states, board_merges(states)))
    engineer_item = roosta_engineer_item(states)
    if engineer_item and engineer_item["id"] not in {item["id"] for item in active}:
        active.insert(0, engineer_item)
    # Prerequisites come from local state, never cached model output.
    active = [item for item in active if item["id"] not in {SETUP_ITEM_ID, AI_SETUP_ITEM_ID}]
    first_run = setup_item()
    if first_run:
        active.insert(0, first_run)
    due_reminders, later_reminders = split_due_reminders(reminders)
    have = {item["id"] for item in active}
    active = [item for item in due_reminders if item["id"] not in have] + active
    have.update(item["id"] for item in active)
    later = [item for item in later_reminders if item["id"] not in have]
    return active, later


# The conductor's picture is only as complete as the sensors feeding its signal
# world. If a sensor task has errored or gone stale, the board can show a false
# all-clear - fewer items, no warning. Sensors are the conductor plus every task
# marked `sensor: true` in tasks.yaml: the watchers gathering the signal it reasons over.
BOARD_SENSOR_IDS = ("assistant",)
BOARD_STALE_FACTOR = 3  # last_run older than N x interval => stale

SERVER_STARTED = time.time()


def restart_pending(started: float | None = None, root: Path | None = None) -> bool:
    """True when a Roosta module changed on disk after this server loaded it.

    An editable install or an upgrade replaces the code under a running runner; HTML and CSS
    reload by themselves, but Python changes wait for a restart, so the board says so.
    """
    started = SERVER_STARTED if started is None else started
    try:
        return any(p.stat().st_mtime > started for p in (root or paths.APP_ROOT).glob("*.py"))
    except OSError:
        return False


# A run that says the GitHub sign-in failed ("gh auth failed", "GitHub auth is broken").
# A dot inside a hostname does not end the sentence.
GITHUB_SIGNIN_LOST = re.compile(
    r"\b(?:github|gh)\b(?:[^.]|\.\S){0,60}?\b(?:auth|token|login)\b"
    r"(?:[^.]|\.\S){0,40}?\b(?:invalid|failed|broken|expired)\b",
    re.IGNORECASE,
)


def board_health(states: list[dict], now: datetime | None = None) -> dict:
    """Report failed board sensors and other scheduled automations.

    Degraded if a sensor last errored or hasn't run in BOARD_STALE_FACTOR x its
    interval. Other failed tasks are reported separately so the Assistant does
    not hide maintenance failures. `ok` remains the sensor-trust verdict.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    degraded = []
    automation_failures = []
    for s in states:
        sid = s.get("id", "")
        if sid not in BOARD_SENSOR_IDS and not s.get("sensor"):
            if (s.get("status") == "error" and sid != "roosta-engineer"
                    and sid):
                automation_failures.append({
                    "id": sid,
                    "nickname": str(s.get("nickname") or "").strip(),
                    "reason": str(s.get("last_error", "")).strip()[:160]
                    or "last run failed",
                })
            continue
        if s.get("status") == "error":
            degraded.append({"id": sid, "nickname": str(s.get("nickname") or "").strip(),
                             "reason": "last run failed"})
            continue
        interval = s.get("interval") or 0
        last_run = s.get("last_run") or ""
        if interval and last_run:
            try:
                last = datetime.fromisoformat(last_run)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if (current - last).total_seconds() > interval * BOARD_STALE_FACTOR:
                    degraded.append({"id": sid, "nickname": str(s.get("nickname") or "").strip(),
                                     "reason": "stale"})
            except ValueError:
                pass  # unparseable timestamp isn't itself a sensor failure
    return {
        "ok": not degraded,
        "degraded": degraded,
        "automation_failures": automation_failures,
        # One expired sign-in fails every scheduled run; name the cause once.
        "claude_signin_expired": any(
            str(s.get("last_error", "")).startswith(CLAUDE_SIGNIN_MESSAGE)
            for s in states if s.get("status") == "error"
        ),
        # Skills word it differently; a network blip without an auth word stays out.
        "github_signin_expired": any(
            GITHUB_SIGNIN_LOST.search(str(s.get("last_error", "")))
            for s in states if s.get("status") == "error"
        ),
    }


def snooze_deadline(preset: str, now: datetime | None = None) -> str:
    """Resolve an allowlisted attention preset to a UTC instant."""
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if preset == "one_hour":
        deadline = current + timedelta(hours=1)
    elif preset == "tomorrow":
        deadline = (current + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0)
    elif preset == "next_monday":
        days = (7 - current.weekday()) % 7 or 7
        deadline = (current + timedelta(days=days)).replace(
            hour=9, minute=0, second=0, microsecond=0)
    else:
        raise ValueError("unknown snooze preset")
    return deadline.astimezone(timezone.utc).isoformat()


def split_snoozed_items(items: list[dict], snoozed: dict,
                         now: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """Partition current conductor items into active and deliberately parked.

    Only items the conductor still believes need the user appear in Later. Expired or
    malformed records fail open and put the item back in the active queue.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    active, later = [], []
    for item in items:
        record = snoozed.get(item.get("id")) if isinstance(snoozed, dict) else None
        until_raw = record.get("until", "") if isinstance(record, dict) else ""
        if not isinstance(until_raw, str):
            active.append(item)
            continue
        try:
            until = datetime.fromisoformat(until_raw.replace("Z", "+00:00"))
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            active.append(item)
            continue
        if until <= current:
            active.append(item)
            continue
        later.append({**item, "snoozed_until": until.astimezone(timezone.utc).isoformat()})
    later.sort(key=lambda item: item["snoozed_until"])
    return active, later


async def _terminate(proc) -> None:
    """Kill a still-running child process group and reap its leader."""
    if proc and proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (AttributeError, ProcessLookupError):
            proc.kill()
        await proc.wait()


def task_idle_timeout(task: TaskConfig, override: float | None = None) -> float:
    """Resolve the silence timeout, allowing longer local-model inference."""
    if override is not None:
        return override
    default = (
        OPENCODE_OLLAMA_IDLE_TIMEOUT
        if task.harness == OPENCODE_OLLAMA_HARNESS
        else TASK_IDLE_TIMEOUT
    )
    return task.idle_timeout or default


def task_prompt(task: TaskConfig, *, run_source: str = "scheduled") -> str:
    """Build the harness prompt, distinguishing manual Run now from the timer."""
    if task.allow_git_push:
        template = (
            GIT_PUSH_MANUAL_PROMPT_TEMPLATE
            if run_source == "manual"
            else GIT_PUSH_SCHEDULED_PUSH_PROMPT_TEMPLATE
            if task.push_on_schedule
            else GIT_PUSH_SCHEDULED_PROMPT_TEMPLATE
        )
        return template.format(skill=task.skill, user=profile.load().user)
    return PROMPT_TEMPLATE.format(skill=task.skill, user=profile.load().user)


async def run_task(
    task: TaskConfig,
    *,
    session_id: str | None = None,
    idle_timeout: float | None = None,
    max_timeout: float | None = None,
    run_source: str = "scheduled",
) -> tuple[str, dict, str, str]:
    """Run the selected harness. Returns (status, output, error, session_id).

    The child is reaped on *inactivity*, not total runtime: harness JSON events
    keep a legitimately long-but-busy pass resetting
    the idle timer and is left to finish. Only a run that goes silent for `idle`
    seconds (presumed hung) is killed - with `maxt` as an absolute backstop against a
    pathological infinite-output loop.
    """
    idle = task_idle_timeout(task, idle_timeout)
    maxt = max_timeout if max_timeout is not None else (task.max_timeout or TASK_MAX_TIMEOUT)
    chat_id = session_id or ""
    prompt = task_prompt(task, run_source=run_source)
    claude_stdin = None
    if task.connection or task.harness == "connection":
        try:
            run = ai_backend.prepare(task.connection, task.dir, session_id=chat_id,
                model=connection_task_model(task, ai_connections.get(task.connection)),
                profile="unrestricted" if task.unrestricted else "git-push" if task.allow_git_push else "safe")
            repo = github_issues.approved_repo(run.connection["id"], task.id, task.skill)
            if repo:
                prompt += (f" For one open ready issue in {repo}, the runner has standing approval "
                           "to post your clarification or completion comment and, on completion, add "
                           "the in review label. Leave the GitHub write to the runner. Request it in "
                           'details.issue_update as {"issue": positive integer, "comment": text, '
                           '"complete": boolean} in a successful result. Omit it when no update is needed.')
            chat_id = run.session_id
            stdout = "".join([line async for line in ai_backend.stream(run, prompt, idle=idle, maximum=maxt)])
        except (ValueError, OSError) as exc:
            return "error", {}, str(exc), chat_id
        text = run.text(stdout)
        output = parse_task_output(text)
        status = TASK_STATUS_ALIASES.get(str(output.get("status", "")).strip().lower(), output.get("status"))
        if "<task-output>" not in text or status not in ("success", "error"):
            return "error", output, "Harness did not return a valid <task-output> result", chat_id
        details = output.get("details")
        if status == "success" and isinstance(details, dict) and "issue_update" in details:
            try:
                if not repo or github_issues.approved_repo(run.connection["id"], task.id, task.skill) != repo:
                    raise ValueError("No current standing approval for this worker's GitHub issue update")
                receipt = await asyncio.to_thread(github_issues.apply_update, repo, details["issue_update"])
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                error = f"GitHub issue update incomplete: {exc}"
                output.update(status="error", summary=f"{output.get('summary', '')} {error}".strip())
                return "error", output, error, chat_id
            output["summary"] = f"{output.get('summary', '')} {receipt}".strip()
        return status, output, "" if status == "success" else str(output.get("summary", ""))[:500], chat_id
    if task.harness == OPENCODE_OLLAMA_HARNESS:
        if task.allow_git_push:
            return (
                "error",
                {},
                "OpenCode using Ollama does not support the privileged git-push profile",
                "",
            )
        cmd = opencode_ollama_args(prompt, workspace=task.dir, model=task.model)
        child_env = opencode_ollama_env(workspace=task.dir, skill=task.skill)
        result_text = opencode_result_text
        chat_id = ""
        has_progress, transient = cursor_stream_has_progress, is_transient_agent_failure
    elif task.harness == CLAUDE_HARNESS:
        # Empty claude_model means CLAUDE_MODEL; Cursor model names never reach Claude.
        chat_id = chat_id or await ai_backend.create_session(task.dir, ai_backend.CLAUDE)
        cmd = claude_args(
            session_id=chat_id,
            model=task.claude_model or None,
            profile="unrestricted" if task.unrestricted
            else "git-push" if task.allow_git_push else "safe",
        )
        claude_stdin = prompt.encode()
        child_env = claude_env()
        # Print mode exits when the turn ends, so a backgrounded command's completion
        # notification never arrives and the run ends without <task-output>.
        child_env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
        # With backgrounding off, Claude's 10-minute foreground Bash cap killed teams-archive's
        # script mid-pass. Let one command run up to the run's ceiling, leaving time to report.
        child_env["BASH_MAX_TIMEOUT_MS"] = str(int(max(600, maxt - 120) * 1000))
        result_text = claude_result_text
        has_progress, transient = claude_stream_has_progress, is_transient_claude_failure
    elif task.harness == CURSOR_HARNESS:
        if not chat_id:
            try:
                chat_id = await ai_backend.create_session(task.dir, ai_backend.CURSOR)
            except Exception as exc:
                log.warning("Task %s: could not create Cursor chat (%s); running without resume",
                            task.id, exc)
        cmd = cursor_args(
            prompt,
            workspace=task.dir,
            session_id=chat_id or None,
            model=task.model,
            force=True,
            approve_mcps=True,
            binary=CURSOR_AGENT,
        )
        child_env = (cursor_env(safe_config=False) if task.unrestricted
                     else cursor_env(allow_git_push=task.allow_git_push))
        result_text = cursor_result_text
        has_progress, transient = cursor_stream_has_progress, is_transient_agent_failure
    else:
        return "error", {}, runtime_unavailable_message(task), ""

    attempts = 2 if task.harness != OPENCODE_OLLAMA_HARNESS else 1
    for attempt in range(attempts):
        proc = None
        stderr_task = None
        stderr_chunks: list[bytes] = []
        try:
            # SECURITY-REVIEW: subprocess uses a fixed local binary and structured
            # arguments. The configured workspace is not interpreted by a shell, stdin
            # is closed (Claude reads its prompt from it first), and credentials remain
            # only in the child environment.
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if claude_stdin else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=task.dir,
                env=child_env,
                start_new_session=True,
            )
            if claude_stdin:
                proc.stdin.write(claude_stdin)
                await proc.stdin.drain()
                proc.stdin.close()

            # Drain stderr concurrently so a full stderr pipe can never block the child
            # (and be misread as an idle stdout / "hang").
            async def _drain_stderr() -> None:
                while True:
                    b = await proc.stderr.read(65536)
                    if not b:
                        return
                    stderr_chunks.append(b)

            stderr_task = asyncio.create_task(_drain_stderr())

            stdout_chunks: list[bytes] = []
            deadline = time.monotonic() + maxt
            last_output = time.monotonic()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    await _terminate(proc)
                    return "error", {}, f"Task exceeded max runtime of {maxt:g}s", chat_id
                try:
                    chunk = await asyncio.wait_for(
                        proc.stdout.read(65536), timeout=min(idle, remaining)
                    )
                except asyncio.TimeoutError:
                    # On macOS the timer keeps counting while the Mac sleeps but
                    # time.monotonic() does not, so every run in flight at lid-close
                    # was killed as "hung" on wake. Only awake silence counts.
                    if time.monotonic() - last_output < idle / 2:
                        continue
                    await _terminate(proc)
                    if time.monotonic() >= deadline:
                        return "error", {}, f"Task exceeded max runtime of {maxt:g}s", chat_id
                    return "error", {}, f"Task produced no output for {idle:g}s - presumed hung", chat_id
                if not chunk:
                    break  # EOF - child is exiting
                stdout_chunks.append(chunk)
                last_output = time.monotonic()

            await proc.wait()
            stdout = b"".join(stdout_chunks)
        except Exception as e:
            await _terminate(proc)
            return "error", {}, str(e), chat_id
        finally:
            if stderr_task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(stderr_task), timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    stderr_task.cancel()

        stderr = b"".join(stderr_chunks)
        if proc.returncode == 0:
            break
        failure = (stderr + stdout).decode(errors="replace")
        made_progress = has_progress(stdout.decode(errors="replace"))
        if attempt == 0 and not made_progress and transient(failure):
            await asyncio.sleep(2)
            continue
        if result_text is claude_result_text and is_claude_auth_failure(failure):
            return "error", {}, CLAUDE_SIGNIN_MESSAGE, chat_id
        # Raw stream JSON is unreadable on the board; prefer stderr, then the model's last words.
        reason = (stderr.decode(errors="replace").strip()
                  or result_text(stdout.decode(errors="replace")).strip())
        return "error", {}, reason[:500] or f"Exited with code {proc.returncode}", chat_id

    full_text = result_text(stdout.decode(errors="replace"))
    if not full_text.strip():
        return "error", {}, "Harness returned no assistant output", chat_id
    output = parse_task_output(full_text)
    if "<task-output>" not in full_text:
        error = "Harness did not return the required <task-output> result"
        if task.harness == OPENCODE_OLLAMA_HARNESS and is_unexecuted_tool_call(full_text):
            error = "OpenCode returned an unexecuted tool call; choose a stronger Ollama model"
        return "error", output, error, chat_id
    # Models drift from the contract ("ok", "complete"); keep the run rather than discard it.
    status = TASK_STATUS_ALIASES.get(str(output.get("status", "")).strip().lower(), output.get("status", "unknown"))
    if status not in ("success", "error"):
        return "error", output, "Harness returned an invalid <task-output> result", chat_id
    # A reported failure explains itself; show that rather than a bare "last run failed".
    error = "" if status == "success" else str(output.get("summary", "")).strip()[:500]
    return status, output, error, chat_id


# Seen 11 Nov 2026: one offline spell failed eight tasks, and daily ones lost their slot until tomorrow.
OFFLINE_FAILURES = (
    "reach the api server",
    "computer went to sleep",
    "failed to reach the cursor api",
    "econnreset",
    "getaddrinfo enotfound",
)
OFFLINE_RETRY_SECONDS = 900.0


def is_offline_failure(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in OFFLINE_FAILURES)


TASK_STATUS_ALIASES = {
    **dict.fromkeys(("success", "ok", "complete", "completed", "done", "succeeded"), "success"),
    # An honest partial pass is still incomplete, so it stays visible as a failure.
    **dict.fromkeys(("error", "failed", "failure", "partial", "incomplete"), "error"),
}


# --- Runner loop ---


async def precheck_unchanged(task: TaskConfig) -> bool:
    """Run a task's cheap pre-check. True only when it says nothing has changed (exit 1).

    Any other outcome, including a missing command or a timeout, runs the skill rather than
    risk missing work.
    """
    argv = shlex.split(task.precheck) if task.precheck else []
    if not argv:
        return False
    code, _, error = await connectors_mod.run_command(argv, PRECHECK_TIMEOUT)
    return error is None and code == 1


PRECHECK_TIMEOUT = 30


class TaskRunner:
    def __init__(
        self,
        *,
        task_loader: Callable[[], list[TaskConfig]] | None = None,
        auth_checker: Callable[[TaskConfig], Awaitable[bool]] | None = None,
        task_executor: Callable[[TaskConfig], Awaitable[tuple[str, dict, str]]] | None = None,
        connector_registry: "connectors_mod.Connectors | None" = None,
        prechecker: Callable[[TaskConfig], Awaitable[bool]] | None = None,
        activity_log=None,
        cache_dir: Path = CACHE_DIR,
        tasks_file: Path = TASKS_FILE,
        startup_delay: float = 2.0,
        poll_interval: float = 30.0,
        auth_retry_interval: float = 60.0,
        weekday_interval: float = 300.0,
        watch_config: bool = True,
        reconcile_interval: float = 30.0,
        offline_retry: float = OFFLINE_RETRY_SECONDS,
    ) -> None:
        self._tasks: list[TaskConfig] = []
        self._states: dict[str, TaskState] = {}
        self._loop_tasks: list[asyncio.Task] = []
        self._triggers: dict[str, asyncio.Event] = {}
        self._cancelled: set[str] = set()
        self._writer_lock = asyncio.Lock()  # serialises vault-writing tasks against each other only
        self._writer_holder: str | None = None
        self._writer_waiting: set[str] = set()
        self._running = False
        self._stop_event = asyncio.Event()
        self._task_loader = task_loader
        self._auth_checker = auth_checker
        self._task_executor = task_executor
        self._connectors = connector_registry
        self._prechecker = prechecker or precheck_unchanged
        self._activity_log = activity_log
        self._cache_dir = Path(cache_dir)
        self._tasks_file = Path(tasks_file)
        self._startup_delay = startup_delay
        self._poll_interval = poll_interval
        self._auth_retry_interval = auth_retry_interval
        self._weekday_interval = weekday_interval
        self._offline_retry = offline_retry
        self._watch_config_enabled = watch_config
        self._reconcile_interval = reconcile_interval

    def start(self) -> None:
        self._tasks = self._load_tasks()
        self._running = True
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        for task in self._tasks:
            self._spawn(task)
        if self._watch_config_enabled:
            self._loop_tasks.append(
                asyncio.create_task(self._watch_config(), name="config-watcher")
            )
        if self._reconcile_interval:
            self._loop_tasks.append(
                asyncio.create_task(self._reconcile_loop(), name="config-reconcile")
            )
        log.info("Task runner started with %d task(s)", len(self._tasks))

    def _load_tasks(self) -> list[TaskConfig]:
        loader = self._task_loader or load_tasks
        return loader()

    def _load_tasks_map(self) -> dict[str, TaskConfig]:
        return {t.id: t for t in self._load_tasks()}

    async def _check_auth(self, task: TaskConfig) -> bool:
        checker = self._auth_checker or check_task_runtime
        return await checker(task)

    async def _run_task(
        self, task: TaskConfig, *, run_source: str = "scheduled",
    ) -> tuple[str, dict, str, str]:
        executor = self._task_executor or run_task
        return await executor(task, run_source=run_source)

    def _activity(self):
        return self._activity_log if self._activity_log is not None else activity

    def _spawn(self, task: TaskConfig) -> None:
        """Register a task's state and start its poll loop."""
        self._states[task.id] = self._load_state(task)
        self._triggers[task.id] = asyncio.Event()
        self._loop_tasks.append(
            asyncio.create_task(self._supervised_loop(task.id), name=f"task-{task.id}")
        )

    async def _supervised_loop(self, task_id: str) -> None:
        """Keep a task's poll loop alive through unexpected errors.

        An unhandled exception used to end the loop silently: the task kept its
        last good status, manual runs queued forever, and the error only surfaced
        as a failed shutdown.
        """
        while self._running:
            try:
                await self._poll_loop(task_id)
                return
            except Exception as exc:
                log.exception("Task %s: poll loop crashed, restarting", task_id)
                state = self._states.get(task_id)
                if state is not None:
                    state.status = "error"
                    state.last_error = f"Scheduler loop crashed and restarted: {exc!r}"
                    self._save_state(state)
                await asyncio.sleep(self._poll_interval)

    def trigger(self, task_id: str) -> bool:
        """Request an immediate run of a task, bypassing its interval gate.

        Returns False if the task isn't running a loop. The poll loop picks
        the event up within its wait and runs as soon as its scheduling gate allows.
        """
        ev = self._triggers.get(task_id)
        if ev is None:
            return False
        ev.set()
        return True

    def cancel_queued(self, task_id: str) -> bool:
        """Drop a manual trigger that has not started running yet."""
        state = self._states.get(task_id)
        ev = self._triggers.get(task_id)
        if state is None or ev is None:
            return False
        if state.status == "running" or not ev.is_set():
            return False
        self._cancelled.add(task_id)
        ev.clear()
        return True

    def set_runtime(self, task_id: str, harness: str, model: str) -> dict | None:
        """Persist a task's harness/model choice for future runs."""
        state = self._states.get(task_id)
        if state is None or not valid_runtime(harness, model):
            return None
        if state.status == "running" or self.is_queued(task_id):
            raise RuntimeError("task is running or queued")
        model_key = runtime_model_key(harness)
        try:
            update_task_runtime(self._tasks_file, task_id, harness, model, model_key)
        except (OSError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc
        state.harness = harness
        setattr(state, model_key, model)
        return self.get_state(task_id)

    def remove(self, task_id: str) -> bool:
        """Fire a worker: drop it from tasks.yaml and the board now; its poll loop exits
        on its next cycle. Raises RuntimeError while it is running or queued."""
        state = self._states.get(task_id)
        if state is None:
            return False
        if state.status == "running" or self.is_queued(task_id):
            raise RuntimeError("cannot fire a worker while it is running or queued")
        try:
            remove_task(self._tasks_file, task_id)
        except (OSError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc
        self._states.pop(task_id, None)
        (self._cache_dir / f"{task_id}.json").unlink(missing_ok=True)
        # The skill's own scratch state, named after the worker; kept while another
        # worker still runs a skill of that name (they share it).
        if not any(t.skill == task_id for t in self._load_tasks()):
            (paths.STATE_DIR / f"{task_id}.json").unlink(missing_ok=True)
            if task_id not in {"cache", "assistant", "pages", ".", ".."}:  # Roosta's own folders
                shutil.rmtree(paths.STATE_DIR / task_id, ignore_errors=True)
        return True

    def set_settings(self, task_id: str, body: dict) -> dict | None:
        """Persist the Skill runner settings form: name, nickname, description,
        schedule and, when changed, harness and model. Raises ValueError for bad
        input and RuntimeError when the task file cannot take the edit."""
        state = self._states.get(task_id)
        if state is None:
            return None
        values, remove = task_settings_changes(body)
        if "connection" in body:
            if state.status == "running" or self.is_queued(task_id):
                raise RuntimeError("AI connection cannot change while the worker is running or queued")
            connection_id = body["connection"]
            if not isinstance(connection_id, str):
                raise ValueError("Choose an AI connection")
            if connection_id:
                selected = ai_connections.get("" if connection_id == "default" else connection_id)
                if not selected.get("tested"):
                    raise ValueError("Test the connection in Settings first")
                values["connection"] = connection_id
            else:
                remove.add("connection")
        connection_model = body.get("connection_model")
        if connection_model is not None:
            if not isinstance(connection_model, str):
                raise ValueError("Choose a model")
            current = values.get("connection", "" if "connection" in remove else state.connection)
            if not current:
                raise ValueError("Choose an AI connection before choosing its model")
            provider = ai_connections.get("" if current == "default" else current, connected=False)["provider"]
            model_field = "claude_model" if provider == "anthropic" else "model"
            if connection_model.strip():
                ai_connections.runtime(provider, connection_model.strip())
                values[model_field] = connection_model.strip()
            elif getattr(state, model_field, ""):
                remove.add(model_field)
        harness = str(body.get("harness", "") or "").strip()
        model = str(body.get("model", "") or "").strip()
        model_key = ""
        if harness or model:
            if not valid_runtime(harness, model):
                raise ValueError("invalid harness or model")
            model_key = runtime_model_key(harness)
            if (harness, model) != (state.harness, getattr(state, model_key)):
                if state.status == "running" or self.is_queued(task_id):
                    raise RuntimeError("harness and model cannot change while the task is running or queued")
                values.update({"harness": harness, model_key: model})
        if values or remove:
            try:
                update_task_fields(self._tasks_file, task_id, values, remove)
            except (OSError, ValueError) as exc:
                raise RuntimeError(str(exc)) from exc
        if "connection" in values or "connection" in remove:
            state.connection = values.get("connection", "")
        for key in ("model", "claude_model"):
            if key in values or key in remove:
                setattr(state, key, values.get(key, ""))
        # Apply now; the poll loop re-reads tasks.yaml on its next cycle anyway.
        for key in ("name", "nickname"):
            if key in values or key in remove:
                setattr(state, key, values.get(key, ""))
        if "description" in values or "description" in remove:
            state.custom_description = values.get("description", "")
            state.description = state.custom_description or skill_description(
                state.skill, next((t.dir for t in self._tasks if t.id == task_id), VAULT_ROOT),
            )
        for key in ("interval", "run_at", "weekdays_only", "days_of_week"):
            if key in values:
                setattr(state, key, values[key])
            elif key in remove:
                setattr(state, key, {"run_at": "", "weekdays_only": False, "days_of_week": []}[key])
        for key in TASK_SETTING_FLAGS:
            if key in values or key in remove:
                setattr(state, key, key in values)
        if model_key and "harness" in values:
            state.harness = harness
            setattr(state, model_key, model)
        return self.get_state(task_id)

    def _spawn_new_tasks(self) -> None:
        """Start a loop for every task id in tasks.yaml that has none yet."""
        try:
            for task in self._load_tasks():
                if task.id not in self._states:
                    log.info("Config changed: new task %s, spawning", task.id)
                    self._spawn(task)
        except Exception as e:
            log.warning("Config reload failed: %s", e)

    async def _watch_config(self) -> None:
        """Spawn loops for new task ids as soon as tasks.yaml changes.

        Edits to existing tasks and removals are handled by each poll loop re-reading config
        every cycle. If the watcher itself fails it logs and restarts, so it can never die
        silently and leave new tasks unstarted.
        """
        from watchfiles import awatch

        while self._running:
            try:
                async for _ in awatch(self._tasks_file, stop_event=self._stop_event):
                    self._spawn_new_tasks()
                return  # stop_event was set
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tasks.yaml watcher failed, restarting")
                await asyncio.sleep(5)

    async def _reconcile_loop(self) -> None:
        """Backstop for missed file events: re-read tasks.yaml on a timer."""
        while self._running:
            await asyncio.sleep(self._reconcile_interval)
            self._spawn_new_tasks()

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        for t in self._loop_tasks:
            t.cancel()
        for t in self._loop_tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._loop_tasks.clear()

    def is_queued(self, task_id: str) -> bool:
        """True once a trigger has been requested but the run hasn't started yet
        (e.g. blocked behind the writer lock)."""
        ev = self._triggers.get(task_id)
        return ev is not None and ev.is_set()

    def waiting_behind(self, task_id: str) -> str:
        """The writer task this one is waiting on, or '' when it is not waiting."""
        return (self._writer_holder or "") if task_id in self._writer_waiting else ""

    def _view(self, s: TaskState) -> dict:
        return {**asdict(s), "queued": self.is_queued(s.id), "waiting_behind": self.waiting_behind(s.id)}

    def get_states(self) -> list[dict]:
        return [self._view(s) for s in self._states.values()]

    def get_state(self, task_id: str) -> dict | None:
        s = self._states.get(task_id)
        return self._view(s) if s else None

    @asynccontextmanager
    async def _writer_slot(self, task_id: str):
        """Hold the writer lock, recording who holds it and who is waiting behind them."""
        self._writer_waiting.add(task_id)
        try:
            async with self._writer_lock:
                self._writer_waiting.discard(task_id)
                self._writer_holder = task_id
                try:
                    yield
                finally:
                    self._writer_holder = None
        finally:
            self._writer_waiting.discard(task_id)

    def _load_state(self, task: TaskConfig) -> TaskState:
        cache_file = self._cache_dir / f"{task.id}.json"
        if cache_file.is_file():
            try:
                data = json.loads(cache_file.read_text())
                state = TaskState(
                    id=task.id,
                    skill=task.skill,
                    interval=task.interval,
                    description=task.description,
                    name=task.name,
                    nickname=task.nickname,
                    custom_description=task.custom_description,
                    harness=task.harness,
                    connection=task.connection,
                    model=task.model,
                    claude_model=task.claude_model,
                    run_at=task.run_at,
                    weekdays_only=task.weekdays_only,
                    days_of_week=task.days_of_week,
                    writer=task.writer,
                    sensor=task.sensor,
                    requires=list(task.requires),
                    status=data.get("status", "idle"),
                    last_run=data.get("last_run", ""),
                    last_duration=data.get("last_duration", 0.0),
                    last_output=data.get("last_output", {}),
                    run_count=data.get("run_count", 0),
                    last_error=data.get("last_error", ""),
                    retry_at=data.get("retry_at", ""),
                )
                # Don't restore "running" status
                if state.status == "running":
                    state.status = "idle"
                return state
            except Exception:
                pass
        return TaskState(
            id=task.id,
            skill=task.skill,
            interval=task.interval,
            description=task.description,
            name=task.name,
            nickname=task.nickname,
            custom_description=task.custom_description,
            harness=task.harness,
            connection=task.connection,
            model=task.model,
            claude_model=task.claude_model,
            run_at=task.run_at,
            weekdays_only=task.weekdays_only,
            days_of_week=task.days_of_week,
            writer=task.writer,
            sensor=task.sensor,
            requires=list(task.requires),
        )

    def _save_state(self, state: TaskState) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = self._cache_dir / f"{state.id}.json"
        cache_file.write_text(json.dumps(asdict(state), indent=2))

    async def _poll_loop(self, task_id: str) -> None:
        # Stagger start by a few seconds per task
        await asyncio.sleep(self._startup_delay)

        while self._running:
            # Re-read config each cycle so interval/skill/dir edits apply live.
            task = self._load_tasks_map().get(task_id)
            if task is None:
                log.info("Task %s: removed from config, stopping loop", task_id)
                self._states.pop(task_id, None)
                return
            state = self._states[task_id]
            state.interval = task.interval
            state.harness = task.harness
            state.connection = task.connection
            state.model = task.model
            state.claude_model = task.claude_model
            state.run_at = task.run_at
            state.weekdays_only = task.weekdays_only
            state.days_of_week = task.days_of_week
            state.writer = task.writer
            state.sensor = task.sensor
            state.requires = list(task.requires)
            state.skill = task.skill
            state.description = task.description
            state.name = task.name
            state.nickname = task.nickname
            state.custom_description = task.custom_description

            triggered = self._triggers[task_id].is_set()

            # Day gate: manual runs bypass calendar restrictions.
            now_local = datetime.now().astimezone()
            day_name = now_local.strftime("%A").lower()
            day_blocked = task.days_of_week and day_name not in task.days_of_week
            weekend_blocked = task.weekdays_only and now_local.weekday() >= 5
            # Off-day catch-up only: on the scheduled day the normal run_at gate decides.
            catch_up = bool(day_blocked) and not weekend_blocked and missed_weekly_slot(
                task.days_of_week, task.run_at, state.last_run, now_local)
            if not triggered and not catch_up and (day_blocked or weekend_blocked):
                await self._wait_or_trigger(task_id, self._weekday_interval)
                continue
            if catch_up and not triggered:
                log.info("Task %s: catching up a missed weekly run", task_id)

            # A run lost to the network or sleep retries soon instead of losing its slot.
            retry_due = bool(state.retry_at) and datetime.now(timezone.utc) >= datetime.fromisoformat(state.retry_at)

            # Check scheduling gate (skip if manually triggered, catching up or retrying).
            if not triggered and not catch_up and not retry_due:
                if task.run_at:
                    # Time-of-day scheduling: run once per day at the specified local time.
                    h, m = map(int, task.run_at.split(":"))
                    target_today = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
                    if now_local < target_today:
                        # Before today's window - sleep until close to it.
                        secs = (target_today - now_local).total_seconds()
                        await self._wait_or_trigger(task_id, min(secs, self._poll_interval))
                        continue
                    if state.last_run:
                        try:
                            last_local = datetime.fromisoformat(state.last_run).astimezone()
                            if last_local >= target_today:
                                # Already ran today - sleep until tomorrow's window.
                                target_tomorrow = target_today + timedelta(days=1)
                                secs = (target_tomorrow - now_local).total_seconds()
                                await self._wait_or_trigger(task_id, min(secs, self._poll_interval))
                                continue
                        except ValueError:
                            pass
                elif state.last_run:
                    # Interval-based scheduling.
                    try:
                        last = datetime.fromisoformat(state.last_run)
                        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
                        if elapsed < task.interval:
                            await self._wait_or_trigger(task_id, self._poll_interval)
                            continue
                    except ValueError:
                        pass

            registry = self._connectors if self._connectors is not None else connectors
            down = await registry.signed_out(task.requires) if registry is not None and task.requires else []
            if down:
                reason = "Waiting for sign-in: " + ", ".join(down)
                if state.status != "blocked" or state.last_error != reason:
                    state.status = "blocked"
                    state.last_error = reason
                    self._save_state(state)
                    log.info("Task %s: %s", task_id, reason)
                # A manual run cannot bypass this gate, so consume its trigger.
                # Left set, _wait_or_trigger returns instantly and the loop spins
                # at full CPU, starving the event loop that serves the dashboard.
                if triggered:
                    self._triggers[task_id].clear()
                await self._wait_or_trigger(task_id, self._auth_retry_interval)
                continue
            if state.status == "blocked":
                # The connector is back; a writer may still wait for the lock, so stop blaming it.
                state.status = "idle"
                state.last_error = ""
                self._save_state(state)

            # A cheap pre-check can confirm nothing changed and skip the model run. The
            # last output carries forward, minus any old error the conductor would re-raise.
            if not triggered and task.precheck:
                started = time.monotonic()
                if await self._prechecker(task):
                    output = dict(state.last_output)
                    details = output.get("details")
                    if isinstance(details, dict) and "error" in details:
                        output["details"] = {k: v for k, v in details.items() if k != "error"}
                    output.update({"summary": "Nothing new since the last run", "status": "success"})
                    state.status = "success"
                    state.last_run = datetime.now(timezone.utc).isoformat()
                    state.last_duration = round(time.monotonic() - started, 1)
                    state.last_output = output
                    state.run_count += 1
                    state.last_error = ""
                    self._save_state(state)
                    log.info("Task %s: pre-check unchanged, skipped agent run", task_id)
                    await self._wait_or_trigger(task_id, self._poll_interval)
                    continue

            # Auth check
            if not await self._check_auth(task):
                state.status = "auth-failed"
                state.last_error = runtime_unavailable_message(task)
                self._save_state(state)
                if triggered:
                    self._triggers[task_id].clear()
                log.info("Task %s: auth failed, skipping", task_id)
                await self._wait_or_trigger(task_id, self._auth_retry_interval)
                continue

            # Run the task. Writers (tasks that rewrite vault docs / auto-commit) are
            # serialised against each other via _writer_lock; everything else runs
            # freely in parallel, including alongside a writer.
            lock_cm = self._writer_slot(task_id) if task.writer else nullcontext()
            async with lock_cm:
                if task_id in self._cancelled:
                    self._cancelled.discard(task_id)
                    self._triggers[task_id].clear()
                else:
                    if task.connection == "default" or (task.harness == "connection" and not task.connection):
                        # Bind once after the writer queue, before activity and execution.
                        task = replace(task, connection=ai_connections.get(connected=False)["id"])
                    source = "manual" if self._triggers[task_id].is_set() else "scheduled"
                    self._triggers[task_id].clear()
                    state.status = "running"
                    state.last_error = ""
                    self._save_state(state)
                    log.info("Task %s: starting (%s)", task_id, source)
                    act_id = self._activity().start(
                        "skill", task.skill, source, task_id=task.id,
                        backend=task_engine(task), connection_id=task.connection,
                    )

                    start = time.monotonic()
                    status, output, error, session_id = await self._run_task(
                        task, run_source=source,
                    )
                    duration = time.monotonic() - start
                    if session_id:
                        self._activity().set_session(act_id, session_id)
                    self._activity().finish(
                        act_id,
                        "error" if status == "error" else "success",
                        outcome_summary(output, error),
                    )

                    state.status = status
                    state.last_run = datetime.now(timezone.utc).isoformat()
                    state.last_duration = round(duration, 1)
                    if task.id == "assistant" and isinstance(output, dict):
                        carry_forward_board_items(
                            output.get("details"),
                            (state.last_output or {}).get("details"),
                        )
                    state.last_output = output
                    state.run_count += 1
                    state.last_error = error
                    state.retry_at = (
                        (datetime.now(timezone.utc) + timedelta(seconds=self._offline_retry)).isoformat()
                        if status == "error" and is_offline_failure(outcome_summary(output, error))
                        else ""
                    )
                    self._save_state(state)
                    log.info("Task %s: finished in %.1fs with status=%s", task_id, duration, status)

            await self._wait_or_trigger(task_id, self._poll_interval)

    async def _wait_or_trigger(self, task_id: str, timeout: float) -> None:
        """Sleep up to `timeout`s, returning early if the task is triggered."""
        ev = self._triggers.get(task_id)
        if ev is None:
            await asyncio.sleep(timeout)
            return
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass


runner = TaskRunner()


# --- Activity log: one place that sees every AI spawn ---


ACTIVITY_FILE = CACHE_DIR / "activity.jsonl"


class ActivityLog:
    """Single window onto every scheduled AI process the dashboard spawns.

    Each spawn calls start(kind, label, source) -> id, then finish(id, status).
    Entries live in memory (last MAX), append to activity.jsonl for durability,
    and publish to subscriber queues so /api/activity/stream is live.

    `status` uses the same words as a task's status - running, success, error -
    so one run reads the same on the runner page as it does in the feed.
    """

    MAX = 200

    # Enough of the tail to rebuild MAX entries; each spawn writes 2-3 lines.
    HYDRATE_BYTES = 512 * 1024

    # The file is append-only, so server startup (lifespan, not import) trims it to its newest whole lines. About two
    # months of runs: enough history to diagnose failures, without growing forever.
    KEEP_BYTES = 4 * 1024 * 1024

    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}      # id -> entry (running + recent)
        self._order: list[str] = []              # insertion order, newest last
        self._subs: set[asyncio.Queue] = set()

    def hydrate(self, path: Path = ACTIVITY_FILE) -> None:
        """Rebuild recent history from the append-only log after a restart.

        The runner restarts often (Roosta.app relaunch, reloader), and an
        in-memory-only feed leaves the activity panel almost empty each time.
        An entry still marked running belonged to the previous process, whose
        harness child died with it, so it is recorded as interrupted rather
        than reappearing as live work that will never finish.
        """
        try:
            with path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - self.HYDRATE_BYTES))
                raw = f.read().decode("utf-8", "replace")
        except OSError:
            return
        lines = raw.splitlines()
        if size > self.HYDRATE_BYTES and lines:
            lines = lines[1:]  # first line is probably a partial record
        collapsed: dict[str, dict] = {}
        for line in lines:
            try:
                entry = json.loads(line)
            except (ValueError, TypeError):
                continue
            eid = entry.get("id")
            if isinstance(eid, str) and eid:
                collapsed[eid] = entry  # later writes supersede earlier ones
        ordered = sorted(collapsed.values(), key=lambda e: str(e.get("started", "")))
        for entry in ordered[-self.MAX :]:
            if entry.get("status") == "running":
                entry["status"] = "interrupted"
            self._entries[entry["id"]] = entry
            self._order.append(entry["id"])

    def compact(self, path: Path = ACTIVITY_FILE) -> None:
        """Keep only the newest KEEP_BYTES of whole lines. Runs before any spawn appends."""
        try:
            if path.stat().st_size <= self.KEEP_BYTES:
                return
            with path.open("rb") as f:
                f.seek(-self.KEEP_BYTES, os.SEEK_END)
                tail = f.read()
            tail = tail[tail.find(b"\n") + 1 :]  # drop the partial first record
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_bytes(tail)
            tmp.replace(path)
        except OSError:
            pass  # a full log is better than a lost one

    def _publish(self, entry: dict) -> None:
        for q in list(self._subs):
            q.put_nowait(dict(entry))

    def start(
        self,
        kind: str,
        label: str,
        source: str,
        *,
        task_id: str = "",
        session_id: str = "",
        backend: str = "",
        connection_id: str = "",
    ) -> str:
        """Record a spawn. kind: skill|chat|action|send. source: scheduled|manual|you.

        `backend` names the engine whose session store holds the transcript.
        """
        eid = uuid.uuid4().hex[:12]
        entry = {
            "id": eid, "kind": kind, "label": label, "source": source,
            "status": "running", "started": datetime.now(timezone.utc).isoformat(),
            "finished": "", "duration": 0.0, "task_id": task_id, "session_id": session_id,
            "backend": backend, "connection_id": connection_id, "summary": "", "_t0": time.monotonic(),
        }
        self._entries[eid] = entry
        self._order.append(eid)
        self._evict()
        self._append(entry)
        self._publish(entry)
        return eid

    def set_session(self, eid: str, session_id: str) -> None:
        entry = self._entries.get(eid)
        if entry is None or not session_id:
            return
        entry["session_id"] = session_id
        self._append(entry)
        self._publish(entry)

    def get(self, eid: str) -> dict | None:
        entry = self._entries.get(eid)
        return self._public(entry) if entry else None

    SUMMARY_CHARS = 400

    def finish(self, eid: str, status: str = "success", summary: str = "") -> None:
        entry = self._entries.get(eid)
        if entry is None:
            return
        entry["status"] = status
        entry["summary"] = " ".join(str(summary or "").split())[: self.SUMMARY_CHARS]
        entry["finished"] = datetime.now(timezone.utc).isoformat()
        entry["duration"] = round(time.monotonic() - entry.pop("_t0", time.monotonic()), 1)
        self._append(entry)
        self._publish(entry)

    def recent(self, limit: int = 50) -> list[dict]:
        ids = self._order[-limit:]
        return [self._public(self._entries[i]) for i in ids if i in self._entries]

    def running(self) -> list[dict]:
        """All in-flight spawns, regardless of recent-window eviction."""
        return [
            self._public(entry)
            for entry in self._entries.values()
            if entry.get("status") == "running"
        ]

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    @staticmethod
    def _public(entry: dict) -> dict:
        return {k: v for k, v in entry.items() if not k.startswith("_")}

    def _evict(self) -> None:
        while len(self._order) > self.MAX:
            old = self._order.pop(0)
            self._entries.pop(old, None)

    def _append(self, entry: dict) -> None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with ACTIVITY_FILE.open("a") as f:
                f.write(json.dumps(self._public(entry)) + "\n")
        except OSError:
            pass  # ponytail: durability is best-effort; never crash a run over a log line


activity = ActivityLog()
activity.hydrate()


# --- Connectors ---

connectors = connectors_mod.Connectors(path=paths.CONNECTORS_FILE)



# --- FastAPI app ---


async def ensure_chat_router() -> bool:
    """Retry starting chats, so installing or signing in to an engine needs no restart."""
    if chat_router is None:
        await _start_chat_router()
    return chat_router is not None


def chat_unavailable() -> JSONResponse:
    """Say which engine is missing and what to do, not just that chats are off."""
    return JSONResponse({"error": f"{ai_backend.unavailable_message()}. Open Settings → AI connections, then try again."},
                        status_code=503)


async def _start_chat_router() -> None:
    """Create the chat router once the selected AI backend is usable."""
    global chat_router
    if chat_router is not None:
        return
    try:
        # History and per-chat connections remain available even when the default is disconnected.
        chat_router = ChatRouter(
            agent,
            SessionMap(SESSIONS_FILE),
            str(VAULT_ROOT),  # chats run at the workspace root
            HANDLED_FILE,
            SNOOZED_FILE,
            TopicStore(TOPICS_FILE),
            ConversationHistoryStore(CHAT_HISTORY_FILE),
            reminders_path=REMINDERS_FILE,
            raised_path=RAISED_FILE,
            merged_path=MERGED_FILE,
        )
        log.info("%s ready", ai_backend.label())
    except Exception as e:
        log.warning("%s unavailable: %s (chats disabled)", ai_backend.label(), e)


# Cursor's CLI writes a per-run SQLite session store under each config dir's `chats/`
# tree. The dashboard never reads these (it renders Cursor history from
# ~/.cursor/projects and Claude history from ~/.claude/projects); they are pure CLI
# scratch that otherwise grows without bound - thousands of stores, many with orphaned
# -wal files from sessions that never closed. Reclaim the old ones at startup, the same
# way activity.jsonl is compacted. ~/.claude and ~/.cursor transcript trees are left
# alone: they are the surfaces the drill-down reads and mingle with the user's own non-dashboard
# CLI history, so pruning them would be real context loss.
SESSION_STORE_RETENTION_DAYS = 30


def prune_cursor_session_stores(
    retention_days: int = SESSION_STORE_RETENTION_DAYS,
    referenced: set[str] | None = None,
) -> int:
    """Delete Cursor CLI session-store dirs older than the retention window.

    Each store is `<config>/chats/<hash>/<session-uuid>/`. A store is kept when its
    newest file is within the window or its uuid is still referenced by the activity
    log (so a resumable chat is never pulled out from under the CLI). Returns the count
    removed. Never raises: a stale store is cheaper than a crashed startup.
    """
    keep = referenced or {e.get("session_id", "") for e in activity._entries.values()}
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for config_dir in (SAFE_CONFIG_DIR, GIT_PUSH_CONFIG_DIR):
        chats = config_dir / "chats"
        if not chats.is_dir():
            continue
        for store in chats.glob("*/*"):
            if not store.is_dir() or store.name in keep:
                continue
            try:
                newest = max((p.stat().st_mtime for p in store.rglob("*")), default=store.stat().st_mtime)
                if newest >= cutoff:
                    continue
                shutil.rmtree(store)
                removed += 1
            except OSError:
                continue
    if removed:
        log.info("pruned %d stale Cursor session store(s)", removed)
    return removed


@asynccontextmanager
async def lifespan(app: FastAPI):
    activity.compact(ACTIVITY_FILE)  # before runner.start(), so no spawn is appending
    prune_cursor_session_stores()    # reclaim unread Cursor CLI scratch, same timing
    try:
        await asyncio.to_thread(ai_connections.offer_system)
    except (ValueError, OSError):
        log.warning("Could not detect an existing Claude Code account; open AI settings to continue")
    runner.start()
    await _start_chat_router()
    yield
    jobs = [task for task in _ai_job_tasks.values() if not task.done()]
    for task in jobs:
        task.cancel()
    await asyncio.gather(*jobs, return_exceptions=True)
    await agent.stop()
    await runner.stop()


app = FastAPI(title="Skill Runner", lifespan=lifespan)


class NoCacheHtmlMiddleware(BaseHTTPMiddleware):
    """Keep the desktop shell nav in sync when dashboard HTML changes."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith(("/runner", "/assistant", "/pulse", "/constellation", "/document", "/workspace", "/office", "/settings", "/changelog", "/pages")):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        elif path.startswith("/static/") and path.endswith((".css", ".js")):
            # Shared styles change with the pages; revalidate against the ETag so
            # a page never renders new markup with a stale control family.
            response.headers["Cache-Control"] = "no-cache"
        return response


app.add_middleware(NoCacheHtmlMiddleware)


@app.get("/api/tasks")
async def api_tasks():
    return JSONResponse(runner.get_states())


@app.get("/api/ai-backend")
async def api_ai_backend():
    return JSONResponse(ai_backend.status())


@app.post("/api/ai-backend")
async def api_set_ai_backend(body: dict):
    """Switch every dashboard AI turn (tasks, chats, sends) between engines."""
    backend = str(body.get("backend", "")).strip() if isinstance(body, dict) else ""
    if backend not in ai_backend.BACKENDS:
        return JSONResponse({"error": "invalid backend"}, status_code=400)
    ai_backend.set_backend(backend)
    log.info("AI backend switched to %s", ai_backend.label(backend))
    await _start_chat_router()
    return JSONResponse(ai_backend.status())


def local_ai_settings(request: Request) -> None:
    """Credential/runtime changes are local-only and reject cross-origin browser requests."""
    if not request.client or request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "AI settings are available only on this computer")
    if request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise HTTPException(403, "Invalid local host")
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403, "Cross-origin AI settings request denied")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Cross-site AI settings request denied")


def same_origin_upload(request: Request) -> None:
    """A raw-body POST is a CORS simple request, so refuse other sites writing files here."""
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403, "Cross-origin upload denied")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Cross-site upload denied")


_ai_jobs: dict[str, dict] = {}
_ai_job_tasks: dict[str, asyncio.Task] = {}


def ai_settings_result(data: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status_code, headers={"Cache-Control": "no-store"})


AFFECTED_CHAT_LIMIT = 10


def connection_affected(connection_id: str) -> dict:
    default = ai_backend.default_connection()
    workers = [t.id for t in load_tasks() if t.connection == connection_id or
               (t.harness == "connection" and t.connection in ("", "default") and default == connection_id)]
    chats = [item for item, selected in chat_router._sessions._connections.items() if selected == connection_id] if chat_router else []
    pending = [run for run, pause in chat_router._paused.items() if pause.get("connection") == connection_id] if chat_router else []
    chat_count = len(chats)
    if chat_router:
        active = chat_router.last_active()
        chats = sorted(chats, key=lambda item: active.get(item, ""), reverse=True)[:AFFECTED_CHAT_LIMIT]
    titles = {item: (chat_router.known_item(item) or {}).get("title", item) for item in chats}
    return {"workers": workers, "chats": chats, "chat_count": chat_count, "chat_titles": titles,
            "pending_approvals": pending}


def workspace_managed_by() -> str:
    if os.environ.get("ASSISTANT_DESKTOP") == "1":
        return "desktop"
    if os.environ.get("XPC_SERVICE_NAME") == cli.LAUNCHD_LABEL or (
            sys.platform.startswith("linux") and os.environ.get("INVOCATION_ID")):
        return "service"
    return "terminal"


def skills_lock() -> dict:
    try:
        data = json.loads((paths.ROOSTA_DIR / skills.LOCK_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def workspace_target(body: dict) -> tuple[Path | None, str]:
    raw = body.get("path")
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 1024:
        return None, "Enter the full path to the folder."
    target = Path(raw.strip()).expanduser()
    if not target.is_absolute():
        return None, "Enter the full path to the folder."
    try:
        target = target.resolve()
    except (OSError, RuntimeError):
        return None, "That folder path cannot be used."
    if target.exists() and not target.is_dir():
        return None, f"{target} is not a folder."
    return target, ""


def captured(call: Callable[[], int]) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = call()
    return code, out.getvalue().strip()


@app.get("/api/settings/workspace", dependencies=[Depends(local_ai_settings)])
async def api_settings_workspace():
    config = cli.read_config()
    port = config.get("port")
    lock = skills_lock()
    entries = lock.get("skills") if isinstance(lock.get("skills"), dict) else {}
    service = await asyncio.to_thread(cli.service_status, paths.WORKSPACE)
    return ai_settings_result({
        "version": __version__, "workspace": str(paths.WORKSPACE),
        "remembered": config.get("workspace") if isinstance(config.get("workspace"), str) else "",
        "host": os.environ.get("ROOSTA_HOST") or "127.0.0.1", "port": paths.PORT,
        "saved_host": config.get("host") if isinstance(config.get("host"), str) else "",
        "saved_port": port if isinstance(port, int) and not isinstance(port, bool) else None,
        "managed_by": workspace_managed_by(), "service": service,
        "skills": {"modified": sorted(n for n, e in entries.items() if isinstance(e, dict) and e.get("modified")),
                   "roosta": str(lock.get("roosta") or "")}})


FOLDER_LIST_LIMIT = 500


def list_folders(directory: Path) -> dict:
    names = []
    for entry in os.scandir(directory):
        if entry.name.startswith("."):
            continue
        try:
            if entry.is_dir():
                names.append(entry.name)
        except OSError:
            continue
    names.sort(key=str.casefold)
    folders = []
    for name in names[:FOLDER_LIST_LIMIT]:
        child = directory / name
        try:
            workspace = (child / ".roosta").is_dir()
        except OSError:
            workspace = False
        folders.append({"name": name, "path": str(child), "workspace": workspace})
    return {"path": str(directory), "parent": str(directory.parent) if directory.parent != directory else None,
            "home": str(Path.home()), "is_workspace": (directory / ".roosta").is_dir(),
            "is_empty": not any(True for _ in os.scandir(directory)),
            "folders": folders, "truncated": len(names) > FOLDER_LIST_LIMIT}


@app.get("/api/settings/folders", dependencies=[Depends(local_ai_settings)])
async def api_settings_folders(path: str = ""):
    if path.strip():
        target, error = workspace_target({"path": path})
        if error:
            return ai_settings_result({"error": error}, 400)
    else:
        target = Path.home()
    if not target.is_dir():
        return ai_settings_result({"error": "That folder does not exist."}, 400)
    try:
        return ai_settings_result(await asyncio.to_thread(list_folders, target))
    except OSError:
        return ai_settings_result({"error": "Roosta cannot read that folder."}, 400)


@app.post("/api/settings/workspace", dependencies=[Depends(local_ai_settings)])
async def api_settings_workspace_set(body: dict):
    action = body.get("action")
    if action not in ("select", "create", "adopt"):
        return ai_settings_result({"error": "Choose select, create or adopt."}, 400)
    target, error = workspace_target(body)
    if error:
        return ai_settings_result({"error": error}, 400)
    git = body.get("git", True) is not False
    message = ""
    if action == "select":
        if not (target / ".roosta").is_dir():
            return ai_settings_result(
                {"error": f"{target} is not a Roosta workspace. Create or adopt it instead."}, 400)
        cli.remember_workspace(target)
    else:
        if action == "adopt" and not target.is_dir():
            return ai_settings_result({"error": f"{target} does not exist, so there is nothing to adopt."}, 400)
        code, message = await asyncio.to_thread(
            captured, lambda: cli.init(target, adopt=action == "adopt", initialise_git=git))
        if code:
            return ai_settings_result({"error": message or "Could not set up the workspace."}, 400)
    return ai_settings_result({"workspace": str(target), "active": str(paths.WORKSPACE),
                               "restart_required": target != paths.WORKSPACE, "message": message})


@app.post("/api/settings/address", dependencies=[Depends(local_ai_settings)])
async def api_settings_address(body: dict):
    if workspace_managed_by() == "desktop":
        return ai_settings_result({"error": "The Roosta app always runs on 127.0.0.1:7667."}, 409)
    host, port = body.get("host"), body.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        return ai_settings_result({"error": "Enter a port between 1024 and 65535."}, 400)
    host = host.strip() if isinstance(host, str) else ""
    if host.lower() == "localhost":
        host, loopback = "localhost", True
    else:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            return ai_settings_result({"error": "Enter an IP address such as 127.0.0.1, or localhost."}, 400)
    if not loopback and body.get("confirm_network") is not True:
        return ai_settings_result({"error": "Confirm that anyone on your network may reach Roosta.",
                                   "needs_confirmation": True}, 400)
    cli.remember_address(host, port)
    return ai_settings_result({"saved_host": host, "saved_port": port, "restart_required": True})


@app.post("/api/settings/service", dependencies=[Depends(local_ai_settings)])
async def api_settings_service():
    if workspace_managed_by() == "desktop":
        return ai_settings_result({"error": "The Roosta app already runs Roosta for you. "
                                   "A background service would start a second runner."}, 409)
    if not (await asyncio.to_thread(cli.service_status, paths.WORKSPACE))["supported"]:
        return ai_settings_result({"error": "Background services are not available on this platform."}, 400)
    code, message = await asyncio.to_thread(captured, lambda: cli.service_install(paths.WORKSPACE, paths.PORT))
    if code:
        return ai_settings_result({"error": message or "Could not install the service."}, 400)
    return ai_settings_result({"service": await asyncio.to_thread(cli.service_status, paths.WORKSPACE),
                               "message": message})


@app.post("/api/settings/skills-sync", dependencies=[Depends(local_ai_settings)])
async def api_settings_skills_sync():
    report = await asyncio.to_thread(skills.sync, paths.CORE_SKILLS_DIR, paths.WORKSPACE)
    return ai_settings_result({"installed": report.installed, "updated": report.updated,
                               "current": report.current, "modified": report.modified})


@app.get("/api/ai/connections", dependencies=[Depends(local_ai_settings)])
async def api_ai_connections(item_id: str = ""):
    try:
        await asyncio.to_thread(ai_connections.offer_system)
        data = ai_connections.load()
        engines = {name: await asyncio.to_thread(ai_runtime.describe, name) for name in ai_runtime.RUNTIMES}
        runtimes = {name: {"version": engine["version"], "installed": engine["installed"]}
                    for name, engine in engines.items()}
        selected, selected_model = "", ""
        if item_id and chat_router:
            sessions = chat_router._sessions
            selected = sessions._connections.get(item_id) or (
                f"legacy:{ai_backend.current()}" if sessions.get(item_id) else ai_backend.default_connection())
            selected_model = sessions.model(item_id)
        data = {**data, "connections": [{**c, "signed_in": ai_backend.signed_in(c)} for c in data["connections"]]}
        onboarding = setup_item()
        missing = setup_gaps() if (onboarding and onboarding["id"] == SETUP_ITEM_ID
                                   and SETUP_ITEM_ID not in (chat_router.handled() if chat_router else {})) else []
        return ai_settings_result({**data, "providers": ai_connections.PROVIDERS, "runtimes": runtimes,
            "engines": engines, "terminal_available": sys.platform == "darwin",
            "workspace_setup_ready": bool(missing), "workspace_setup_missing": missing,
            "jobs": _ai_jobs, "selected": selected, "selected_model": selected_model,
            "legacy": ai_backend.current(),
            "affected": {c["id"]: connection_affected(c["id"]) for c in data["connections"]}})
    except ValueError as exc:
        return ai_settings_result({"error": str(exc)}, 400)


async def ollama_model_ids() -> list[str]:
    stdout = await _runtime_command_output(OLLAMA, "list", env=opencode_ollama_env())
    return [m["id"] for m in parse_ollama_models(stdout)]


async def provider_models(provider: str, general: bool = False, key: str = "", refresh: bool = False) -> dict:
    default = ai_connections.default_model(provider, general=general)
    if provider == "anthropic":
        listing = await ai_backend.claude_models(key or "legacy:claude", refresh=refresh)
        models = [ai_backend.claude_model_choice(entry) for entry in listing["models"]]
        member = ai_backend.claude_member(listing, default) if default else None
        label = ((member or {}).get("displayName") or default) if default else ai_backend.default_label(listing["default"])
        shown = member or listing["default"] or {}
        result = {"default": default, "default_label": label, "models": models,
                  "default_efforts": shown.get("supportedEffortLevels") or []}
        if listing.get("fallback"):
            result["error"] = listing["error"]
        return result
    if provider == "ollama":
        return {"default": "", "default_label": "", "models": [{"id": m, "label": m} for m in await ollama_model_ids()]}
    models = []
    for model in await asyncio.to_thread(ai_runtime.model_catalog, provider):
        try:
            ai_connections.runtime(provider, model)
        except ValueError:
            continue
        models.append({"id": model, "label": model})
    default = default if any(m["id"] == default for m in models) else ""
    return {"default": default, "default_label": default, "models": models}


async def validate_model(provider: str, model: str, key: str = "", current: str = "") -> None:
    """A new model choice must be in the harness's own list; an already-stored valid one stays accepted."""
    if not model:
        return
    ai_connections.runtime(provider, model)
    if model == current:
        return
    if provider == "anthropic":
        listing = await ai_backend.claude_models(key or "legacy:claude")
        name, _, effort = model.partition("@")
        if not name and effort:
            return
        if ai_backend.claude_member(listing, model) is None:
            raise ValueError("Claude Code doesn't offer that model; choose one from the list")
        return
    try:
        available = await ollama_model_ids() if provider == "ollama" else await asyncio.to_thread(ai_runtime.model_catalog, provider)
    except (ValueError, OSError):
        return
    if model not in available:
        raise ValueError("That model isn't offered by this provider; choose one from the list")


@app.get("/api/ai/models", dependencies=[Depends(local_ai_settings)])
async def api_ai_models(provider: str = "", connection: str = "", general: int = 0, refresh: int = 0):
    try:
        if connection == "legacy:cursor":
            return ai_settings_result({"default": "", "models": []})
        if connection == "legacy:claude":
            provider = "anthropic"
        elif connection:
            selected = ai_connections.get("" if connection == "default" else connection, connected=False)
            provider, connection = selected["provider"], selected["id"]
        if provider not in ai_connections.PROVIDERS:
            raise ValueError("Choose a provider or connection")
        return ai_settings_result(await provider_models(provider, bool(general), connection, bool(refresh)))
    except (ValueError, OSError) as exc:
        return ai_settings_result({"error": str(exc)}, 400)


@app.post("/api/ai/connections", dependencies=[Depends(local_ai_settings)])
async def api_ai_connection_create(body: dict):
    try:
        return ai_settings_result(ai_connections.create(body), 201)
    except ValueError as exc:
        return ai_settings_result({"error": str(exc)}, 400)
    except Exception:
        return ai_settings_result({"error": "The OS credential store could not save this key; unlock it and try again"}, 400)


async def model_efforts(connection: dict, model: str) -> list[str]:
    if connection["provider"] == "anthropic":
        listing = await ai_backend.claude_models(connection["id"])
        member = ai_backend.claude_member(listing, model)
        return (member or {}).get("supportedEffortLevels") or []
    return await asyncio.to_thread(ai_runtime.model_efforts, connection["provider"], model)


@app.get("/api/ai/efforts", dependencies=[Depends(local_ai_settings)])
async def api_ai_efforts(connection: str, model: str):
    try:
        if not model:
            raise ValueError("Choose a model")
        selected = ai_connections.get(connection)
        await validate_model(selected["provider"], model, selected["id"])
        return ai_settings_result({"levels": await model_efforts(selected, model)})
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        return ai_settings_result({"error": str(exc)}, 400)


@app.post("/api/ai/default", dependencies=[Depends(local_ai_settings)])
async def api_ai_default(body: dict):
    try:
        model = body.get("model")
        effort = body.get("effort", "")
        if not isinstance(effort, str) or (effort and model is None):
            raise ValueError("Choose a model and a supported effort")
        if model is not None:
            if not isinstance(model, str) or not model.strip():
                raise ValueError("Choose a model")
            model = model.strip()
            connection = ai_connections.get(body.get("connection", ""))
            await validate_model(connection["provider"], model, connection["id"])
            if effort and effort not in await model_efforts(connection, model):
                raise ValueError("This model does not support that effort")
        if chat_router:
            for item in list(chat_router._sessions._map):
                chat_router._sessions.connection(item)
        ai_connections.set_default(body.get("connection", ""), model, effort)
        await ensure_chat_router()
        return ai_settings_result({"ok": True})
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        return ai_settings_result({"error": str(exc)}, 400)


@app.post("/api/ai/chat-connection", dependencies=[Depends(local_ai_settings)])
async def api_ai_chat_connection(body: dict):
    await ensure_chat_router()
    item_id, connection_id = body.get("item_id"), body.get("connection")
    if not isinstance(item_id, str) or not 1 <= len(item_id) <= 200:
        return ai_settings_result({"error": "Choose a chat"}, 400)
    if item_id in chat_router.active_items() or any(p["item_id"] == item_id for p in chat_router._paused.values()):
        return ai_settings_result({"error": "Finish this turn or its pending approval before changing connection"}, 409)
    try:
        selected = ai_connections.get(connection_id)
        if not selected.get("tested"):
            raise ValueError("Test this connection first")
        chat_router._sessions.bind(item_id, selected["id"])
        return ai_settings_result({"ok": True, "message": "Connection changed. Visible history and summary are retained; execution starts a new session."})
    except ValueError as exc:
        return ai_settings_result({"error": str(exc)}, 400)


@app.post("/api/ai/chat-model", dependencies=[Depends(local_ai_settings)])
async def api_ai_chat_model(body: dict):
    await ensure_chat_router()
    item_id, model = body.get("item_id"), body.get("model", "")
    if not isinstance(item_id, str) or not 1 <= len(item_id) <= 200 or not isinstance(model, str):
        return ai_settings_result({"error": "Choose a chat and a model"}, 400)
    if item_id in chat_router.active_items():
        return ai_settings_result({"error": "Finish this turn before changing the model"}, 409)
    model = model.strip()
    try:
        if model:
            selected = chat_router._sessions.connection(item_id)
            if selected == "legacy:cursor":
                raise ValueError("Choose a model after switching this chat to a connection")
            current = chat_router._sessions.model(item_id)
            if selected == "legacy:claude":
                await validate_model("anthropic", model, "legacy:claude", current)
            else:
                await validate_model(ai_connections.get(selected, connected=False)["provider"], model, selected, current)
        chat_router._sessions.set_model(item_id, model)
        return ai_settings_result({"ok": True, "model": model})
    except ValueError as exc:
        return ai_settings_result({"error": str(exc)}, 400)


async def validate_claude_task_model(task_id: str, harness: str, model: str) -> None:
    """Cursor model names must not reach Claude: a new Claude pick has to be in Claude Code's own list."""
    if harness == CLAUDE_HARNESS and model:
        existing = runner.get_state(task_id) if runner else None
        await validate_model("anthropic", model, "legacy:claude", (existing or {}).get("claude_model", ""))


async def _ai_operation(key: str, operation: str) -> None:
    proc = None
    terminal = None
    try:
        if operation == "install":
            await asyncio.to_thread(ai_runtime.install, key)
        elif operation == "test":
            await ai_backend.test_connection(key)
            await ensure_chat_router()
        else:
            connection = ai_connections.get(key)
            if connection["provider"] not in {"anthropic", "openai"} or connection["auth"] != "cli":
                raise ValueError("Choose an OpenAI or Anthropic account sign-in connection")
            binary = ai_runtime.find(ai_connections.engine(connection["provider"]))
            if not binary:
                raise ValueError("Install the AI runtime first")
            env = ai_backend.connection_env(connection)
            if connection["provider"] == "openai":
                # Pi requires a terminal; its browser callback completes sign-in without pasted tokens.
                terminal, slave = os.openpty()
                os.set_blocking(terminal, False)
                def discard_auth_output():
                    try:
                        if os.read(terminal, 65536):
                            return
                    except BlockingIOError:
                        return
                    except OSError:
                        pass
                    asyncio.get_running_loop().remove_reader(terminal)
                asyncio.get_running_loop().add_reader(terminal, discard_auth_output)
                try:
                    proc = await asyncio.create_subprocess_exec(binary, "auth", "login", "openai",
                        cwd=env["PI_CODING_AGENT_DIR"], env=env, stdin=slave, stdout=slave, stderr=slave,
                        start_new_session=True)
                finally:
                    os.close(slave)
            else:
                proc = await asyncio.create_subprocess_exec(binary, "auth", "login", "--sso",
                    env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
            try:
                await asyncio.wait_for(proc.wait(), 300)
            except asyncio.TimeoutError:
                raise ValueError("Sign-in timed out; retry in a browser on this computer and close any other pending AI sign-in") from None
            if proc.returncode:
                raise ValueError("Sign-in did not complete; close other pending AI sign-ins, retry and check your organisation's account access")
        _ai_jobs[key] = {"status": "success", "operation": operation}
    except asyncio.CancelledError:
        _ai_jobs[key] = {"status": "interrupted", "operation": operation}
        raise
    except Exception as exc:
        log.exception("AI %s operation failed", operation)
        if isinstance(exc, ValueError):
            reason = str(exc)
        elif isinstance(exc, ssl.SSLError) or (isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, ssl.SSLError)):
            reason = "Download failed: this computer's security certificates couldn't be verified"
        elif isinstance(exc, urllib.error.URLError) and operation == "install":
            reason = f"Download failed: {exc.reason}"
        elif isinstance(exc, OSError) and operation == "install":
            reason = f"Download failed: {exc}"
        else:
            reason = "Operation failed; check network access and the OS credential store, then retry"
        _ai_jobs[key] = {"status": "error", "operation": operation, "error": reason}
    finally:
        if operation == "install":
            ai_runtime.INSTALLING.discard(key)
        await ai_backend.terminate(proc)
        if terminal is not None:
            asyncio.get_running_loop().remove_reader(terminal)
            os.close(terminal)


@app.post("/api/ai/operations", dependencies=[Depends(local_ai_settings)])
async def api_ai_operation(body: dict):
    key, operation = body.get("id"), body.get("operation")
    try:
        if operation == "install":
            if key not in ai_runtime.RUNTIMES:
                raise ValueError("Unknown runtime")
            if any(ai_backend.ACTIVE_CONNECTIONS.values()):
                raise ValueError("Wait for current AI turns to finish before repairing a runtime")
        elif operation in {"test", "login"}:
            connection = ai_connections.get(key)
            if operation == "login" and connection["auth"] == "system":
                raise ValueError("This connection uses your own Claude Code sign-in; run `claude` in a terminal to sign in")
            if operation == "login" and (ai_backend.ACTIVE_CONNECTIONS[key] or connection_affected(key)["pending_approvals"]):
                raise ValueError("Finish current turns and pending approvals before signing this account in again")
            if operation == "login" and connection["provider"] == "openai" and any(
                job.get("operation") == "login" and job.get("status") == "running"
                and ai_connections.get(other)["provider"] == "openai" for other, job in _ai_jobs.items()
            ):
                raise ValueError("Finish the other OpenAI sign-in before starting this one")
        else:
            raise ValueError("Unknown operation")
        if key in _ai_job_tasks and not _ai_job_tasks[key].done():
            return ai_settings_result({"error": "This operation is already running"}, 409)
        if operation == "install":
            ai_runtime.INSTALLING.add(key)
        elif operation == "login":
            ai_connections.update(key, tested=False)
        _ai_jobs[key] = {"status": "running", "operation": operation}
        _ai_job_tasks[key] = asyncio.create_task(_ai_operation(key, operation))
        return ai_settings_result({"status": "running"}, 202)
    except ValueError as exc:
        return ai_settings_result({"error": str(exc)}, 400)


@app.post("/api/ai/terminal", dependencies=[Depends(local_ai_settings)])
async def api_ai_terminal(body: dict):
    runtime = body.get("id")
    if runtime not in ("claude", "pi"):
        return ai_settings_result({"error": "Unknown AI software"}, 400)
    if sys.platform != "darwin":
        return ai_settings_result({"error": "Opening Terminal is available on macOS"}, 400)
    binary = await asyncio.to_thread(ai_runtime.find, runtime)
    if not binary:
        return ai_settings_result({"error": "Install this AI software first"}, 400)
    if runtime in ai_runtime.INSTALLING:
        return ai_settings_result({"error": "Wait for installation to finish"}, 409)
    # Pass shell-quoted paths as data to a fixed AppleScript; never inject account credentials.
    command = f"cd -- {shlex.quote(str(paths.WORKSPACE))} && exec {shlex.quote(binary)}"
    script = '\n'.join(['on run argv', 'tell application "Terminal"', 'activate',
                        'do script (item 1 of argv)', 'end tell', 'return "opened"', 'end run'])
    result = await _runtime_command_output("/usr/bin/osascript", "-e", script, command)
    if result.strip() != "opened":
        return ai_settings_result({"error": "Could not open Terminal; check macOS automation permissions and try again"}, 400)
    return ai_settings_result({"ok": True})


@app.post("/api/ai/connections/{connection_id}/change", dependencies=[Depends(local_ai_settings)])
async def api_ai_change(connection_id: str, body: dict):
    affected = connection_affected(connection_id)
    if (ai_backend.ACTIVE_CONNECTIONS[connection_id] or affected["pending_approvals"] or
            _ai_jobs.get(connection_id, {}).get("status") == "running"):
        return ai_settings_result({"error": "Finish current turns, setup operations and pending approvals before changing this connection"}, 409)
    try:
        return ai_settings_result(ai_connections.change(connection_id, body))
    except ValueError as exc:
        return ai_settings_result({"error": str(exc)}, 400)
    except Exception:
        return ai_settings_result({"error": "Could not update the OS credential store; unlock it and retry"}, 400)


@app.post("/api/ai/connections/{connection_id}/disconnect", dependencies=[Depends(local_ai_settings)])
async def api_ai_disconnect(connection_id: str):
    if ai_backend.ACTIVE_CONNECTIONS[connection_id] or _ai_jobs.get(connection_id, {}).get("status") == "running":
        return ai_settings_result({"error": "Wait for this account's current turn or setup operation to finish"}, 409)
    try:
        connection = ai_connections.get(connection_id, connected=False)
        affected = connection_affected(connection_id)
        if connection["auth"] == "api_key":
            ai_connections._vault().delete_password("Roosta AI", connection["credential_ref"])
        if connection["auth"] == "cli":
            binary = ai_runtime.find(ai_connections.engine(connection["provider"]))
            if not binary:
                raise ValueError("Repair the runtime before signing this account out")
            ai_connections.update(connection_id, connected=False, tested=False)
            env = ai_backend.connection_env(connection)
            args = [binary, "auth", "logout"] + (["openai"] if connection["provider"] == "openai" else [])
            proc = await asyncio.create_subprocess_exec(*args, env=env, cwd=env["PI_CODING_AGENT_DIR"],
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
            try:
                await asyncio.wait_for(proc.wait(), 20)
                if proc.returncode:
                    raise ValueError("The account could not sign out; retry")
            finally:
                await ai_backend.terminate(proc)
        ai_connections.update(connection_id, connected=False, tested=False)
        return ai_settings_result({"ok": True, "affected": affected})
    except ValueError as exc:
        return ai_settings_result({"error": str(exc)}, 400)
    except Exception:
        return ai_settings_result({"error": "The credential store could not remove this key; unlock it and retry"}, 400)


@app.get("/api/runtime-options")
async def api_runtime_options():
    return JSONResponse(await available_runtime_options())


@app.get("/api/tasks/{task_id}")
async def api_task(task_id: str):
    state = runner.get_state(task_id)
    if not state:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(state)


@app.get("/api/tasks/{task_id}/skill")
async def api_task_skill(task_id: str):
    """Preview a configured worker's skill using the runner's lookup order."""
    task = load_tasks_map().get(task_id)
    if task is None:
        return JSONResponse({"error": "Worker not found"}, status_code=404)
    files = skill_files(task.skill, task.dir)
    if not files:
        return JSONResponse({"error": "SKILL.md not found"}, status_code=404)
    candidate = files[0]
    try:
        _, content, _ = read_document(candidate.parent.parent, f"{task.skill}/SKILL.md")
    except (OSError, ValueError):
        return JSONResponse({"error": "SKILL.md could not be previewed; it must be UTF-8 Markdown within its skill root and under 2 MB"}, status_code=400)
    return JSONResponse({"skill": task.skill, "content": content}, headers={"Cache-Control": "no-store"})


@app.post("/api/tasks/{task_id}/runtime")
async def api_task_runtime(task_id: str, body: dict):
    """Persist the harness and model used by this task's future runs."""
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid request"}, status_code=400)
    harness = str(body.get("harness", "")).strip()
    model = str(body.get("model", "")).strip()
    if not valid_runtime(harness, model):
        return JSONResponse({"error": "invalid harness or model"}, status_code=400)
    try:
        await validate_claude_task_model(task_id, harness, model)
        state = runner.set_runtime(task_id, harness, model)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    if state is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(state)


@app.post("/api/tasks/{task_id}/settings")
async def api_task_settings(task_id: str, body: dict, request: Request = None):
    """Persist a task's name, nickname, description, schedule, harness and model."""
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid request"}, status_code=400)
    if "connection" in body and request is not None:
        local_ai_settings(request)
    try:
        await validate_claude_task_model(task_id, str(body.get("harness", "") or "").strip(),
                                         str(body.get("model", "") or "").strip())
        if isinstance(body.get("connection_model"), str) and body["connection_model"].strip():
            existing = runner.get_state(task_id)
            chosen = body["connection"] if isinstance(body.get("connection"), str) else (existing or {}).get("connection", "")
            if existing and chosen:
                connection = ai_connections.get("" if chosen == "default" else chosen, connected=False)
                current = (existing.get("claude_model") if connection["provider"] == "anthropic" else existing.get("model")) or ""
                await validate_model(connection["provider"], body["connection_model"].strip(), connection["id"], current)
        state = runner.set_settings(task_id, body)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    if state is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(state)


@app.delete("/api/tasks/{task_id}")
async def api_task_delete(task_id: str):
    """Fire a worker: remove it from tasks.yaml with its caches, state and 1:1 chat.
    Its skill and anything it wrote to the vault stay."""
    try:
        removed = runner.remove(task_id)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    if not removed:
        return JSONResponse({"error": "not found"}, status_code=404)
    if chat_router is not None:
        chat_router.forget(f"{WORKER_CHAT_PREFIX}{task_id}")
        attachments.remove(UPLOADS_DIR, f"{WORKER_CHAT_PREFIX}{task_id}")
    return JSONResponse({"removed": True})


@app.post("/api/tasks/{task_id}/run")
async def api_task_run(task_id: str):
    """Trigger an immediate run of a task, bypassing its interval gate."""
    if not runner.trigger(task_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"triggered": True})


@app.post("/api/tasks/{task_id}/cancel")
async def api_task_cancel(task_id: str):
    """Drop a queued manual run before it starts."""
    if task_id not in runner._states:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not runner.cancel_queued(task_id):
        return JSONResponse({"error": "not queued"}, status_code=409)
    return JSONResponse({"cancelled": True})


@app.get("/api/activity")
async def api_activity(limit: int = 50):
    """Recent AI spawns (newest last). Snapshot for first paint / no-SSE clients."""
    return JSONResponse(activity.recent(limit))


@app.post("/api/usage")
async def api_usage(body: dict):
    """Record coarse local product interactions without content or item identifiers."""
    try:
        product_usage.record(body)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"recorded": True})


@app.get("/api/activity/stream")
async def api_activity_stream():
    """Live SSE feed of every AI spawn: replays recent, then streams changes."""
    q = activity.subscribe()

    async def gen():
        try:
            for entry in activity.recent(50):
                yield f"data: {json.dumps(entry)}\n\n"
            while True:
                entry = await q.get()
                yield f"data: {json.dumps(entry)}\n\n"
        finally:
            activity.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/activity/{activity_id}")
async def api_activity_entry(activity_id: str):
    entry = activity.get(activity_id)
    if entry is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(entry)


@app.get("/api/activity/{activity_id}/transcript")
async def api_activity_transcript(activity_id: str):
    entry = activity.get(activity_id)
    if entry is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    session_id = entry.get("session_id", "")
    if not valid_session_id(session_id):
        return JSONResponse({"error": "no session"}, status_code=404)
    if entry.get("connection_id"):
        directory = ai_connections.directory(entry["connection_id"])
        turns = (read_claude_transcript(session_id, directory / "claude/projects")
                 if entry.get("backend") == ai_backend.CLAUDE else pi_agent.read_transcript(directory / "pi", session_id))
        resume = "Continue this conversation in Roosta using its original connection"
    elif entry.get("backend") == ai_backend.CLAUDE or claude_session_exists(session_id):
        turns = read_claude_transcript(session_id)
        resume = f"cd {shlex.quote(str(VAULT_ROOT))} && claude --resume {session_id}"
    else:
        turns = read_cursor_transcript(session_id)
        resume = (
            f"CURSOR_CONFIG_DIR={CACHE_DIR / 'cursor-agent-config'} "
            f"agent --resume {session_id}"
        )
    return JSONResponse({
        "activity_id": activity_id,
        "session_id": session_id,
        "turns": turns,
        "outcome": entry.get("summary") or transcript_outcome(turns),
        "resume_command": resume,
    })


def transcript_outcome(turns: list[dict]) -> str:
    """The <task-output> summary from a run's last assistant turn, for rows logged before outcomes were."""
    for turn in reversed(turns):
        if turn.get("role") == "assistant" and "<task-output>" in turn.get("text", ""):
            return outcome_summary(parse_task_output(turn["text"]), "")
    return ""


@app.get("/")
async def index(request: Request):
    """The Assistant is the landing page; a ?task= deep link still opens the Skill runner."""
    if request.url.query:
        return RedirectResponse(f"/runner?{request.url.query}", status_code=307)
    return RedirectResponse("/assistant", status_code=307)


# --- Custom pages ---
#
# A page is static HTML and JS in .roosta/pages/<name>/, served at /pages/<name>/. Pages read the
# generic read-only APIs below plus JSON that scheduled skills write to .roosta/state/pages/<name>/.
# A workspace page named like a shipped one (assistant, pulse, office, ...) replaces it.

PAGE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
PAGE_ASSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,80}){0,3}")
PAGE_DATA_DIR = paths.STATE_DIR / "pages"
PAGE_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json",
              ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".woff2": "font/woff2"}
PAGE_FILES_LIMIT = 500


def page_file(name: str, pages_dir: Path | None = None) -> Path:
    """The workspace's own version of a shipped page if it has one, else Roosta's."""
    override = (pages_dir or paths.PAGES_DIR) / name / "index.html"
    return override if override.is_file() else DASHBOARD_ROOT / "static" / f"{name}.html"


def _contained(root: Path, relative: str) -> Path | None:
    """Resolve a validated relative path inside root, refusing symlink escapes."""
    if not PAGE_ASSET.fullmatch(relative) or ".." in relative.split("/"):
        return None
    base = root.resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target if target.is_file() else None


# Folders with these names replace a shipped page (page_file), so they already have a nav link.
SHIPPED_PAGES = frozenset({"assistant", "index", "workspace", "pulse", "office", "document", "changelog", "settings"})


def custom_pages(pages_dir: Path | None = None) -> list[dict]:
    """Every custom page with an index.html, titled from its <title> tag. Shipped-page overrides are excluded."""
    root = pages_dir or paths.PAGES_DIR
    pages = []
    for folder in sorted(root.iterdir()) if root.is_dir() else []:
        index = folder / "index.html"
        if not PAGE_NAME.fullmatch(folder.name) or folder.name in SHIPPED_PAGES or not index.is_file():
            continue
        match = re.search(r"<title>(.*?)</title>", index.read_text(encoding="utf-8", errors="ignore")[:4000], re.S | re.I)
        title = re.sub(r"\s+", " ", match.group(1)).strip() if match else folder.name.replace("-", " ").title()
        pages.append({"name": folder.name, "title": title[:40], "url": f"/pages/{folder.name}/"})
    return pages


def vault_files(pattern: str, root: Path | None = None) -> list[dict]:
    """Workspace Markdown matching a glob, with titles and frontmatter but no bodies."""
    root = (root or VAULT_ROOT).resolve()
    if not pattern or pattern.startswith("/") or ".." in pattern.split("/") or len(pattern) > 200:
        raise ValueError("invalid pattern")
    out = []
    for path in sorted(root.glob(pattern)):
        try:
            relative = path.relative_to(root).as_posix()
            resolved = resolve_vault_markdown(relative, root)
        except (ValueError, FileNotFoundError):
            continue
        if not workspace_visible(Path(relative)):
            continue
        text = resolved.read_text(encoding="utf-8", errors="ignore")
        out.append({
            "path": relative,
            "title": markdown_title(text, path.stem),
            "frontmatter": workspace_metadata(text),
            "modified": datetime.fromtimestamp(resolved.stat().st_mtime, timezone.utc).isoformat(),
        })
        if len(out) >= PAGE_FILES_LIMIT:
            break
    return out


@app.get("/api/pages")
async def api_pages():
    return JSONResponse(custom_pages())


@app.get("/api/vault/files")
async def api_vault_files(glob: str = "**/*.md"):
    """Read-only: Markdown files matching a glob, with frontmatter, for custom pages."""
    try:
        files = await asyncio.to_thread(vault_files, glob)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(files, headers={"Cache-Control": "no-store"})


@app.get("/api/pages/{name}/data/{file}")
async def api_page_data(name: str, file: str):
    """JSON a scheduled skill wrote for a page, from .roosta/state/pages/<name>/<file>."""
    if not PAGE_NAME.fullmatch(name) or not file.endswith(".json"):
        return JSONResponse({"error": "not found"}, status_code=404)
    target = _contained(PAGE_DATA_DIR / name, file)
    if target is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(target, media_type="application/json", headers={"Cache-Control": "no-store"})


@app.get("/pages/{name}/{asset:path}")
async def custom_page(name: str, asset: str = ""):
    if not PAGE_NAME.fullmatch(name):
        return JSONResponse({"error": "not found"}, status_code=404)
    target = _contained(paths.PAGES_DIR / name, asset or "index.html")
    if target is None or target.suffix.lower() not in PAGE_TYPES:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(target, media_type=PAGE_TYPES[target.suffix.lower()],
                        headers={"Cache-Control": "no-cache"})


@app.get("/pages/{name}")
async def custom_page_root(name: str):
    return RedirectResponse(f"/pages/{name}/")


OFFICE_CAST_FILE = paths.ROOSTA_DIR / "office-cast.yaml"
LPC_DIR = paths.STATIC_DIR / "vendor" / "lpc"


def office_cast(path: Path = OFFICE_CAST_FILE, lpc: Path = LPC_DIR) -> dict:
    """Workspace looks for Office workers, keyed by lower-case nickname or task id.

    Only looks whose every sprite layer is vendored pass, so a typo cannot break the page.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    cast = {}
    for name, look in (raw.items() if isinstance(raw, dict) else []):
        if not isinstance(look, dict):
            continue
        look = {k: str(v) for k, v in look.items()
                if k in {"body", "head", "tone", "hair", "beard", "glasses"} and isinstance(v, str)}
        body, tone = look.get("body"), look.get("tone", "")
        suffix = "-f" if body == "f" else ""
        head = {"m": "head", "f": "head-f", "old": "head-old"}.get(look.get("head") or body or "")
        needed = [f"body{suffix}/{tone}", f"{head}/{tone}", f"hair/{look.get('hair', '')}"]
        needed += [f"hair/{look['beard']}"] if look.get("beard") else []
        needed += [f"glasses/{look['glasses']}"] if look.get("glasses") else []
        safe = all(re.fullmatch(r"[A-Za-z0-9 _/-]+", n) and ".." not in n for n in needed)
        if body in {"m", "f"} and head and safe and all((lpc / n / "Walk.png").is_file() for n in needed):
            cast[str(name).strip().lower()] = look
    return cast


@app.get("/api/office/cast")
async def api_office_cast():
    return JSONResponse(office_cast())


@app.get("/api/profile")
async def api_profile():
    """Who Roosta works for, from PROFILE.md, so pages can say their name."""
    return JSONResponse(profile.load().public())


CHAT_IDEAS_FILE = ASSISTANT_DIR / "chat-ideas.json"
CHAT_IDEA_KINDS = {"focus", "project", "tool", "remember", "build", "learn"}
CHAT_IDEAS_MAX = 8


def load_chat_ideas(path: Path | None = None) -> dict:
    """Starter prompts the `chat-ideas` skill wrote from this workspace, validated.

    The file is model-written, so anything malformed is dropped rather than shown: an idea
    is a non-empty one-line `text` (at most 240 characters) with an optional short `label`
    and a `kind` from CHAT_IDEA_KINDS. No usable ideas means the page keeps its built-in ones.
    """
    try:
        raw = json.loads((path or CHAT_IDEAS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ideas": [], "generated": None}
    entries = raw.get("ideas") if isinstance(raw, dict) else None
    ideas, seen = [], set()
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, str):
            entry = {"text": entry}
        if not isinstance(entry, dict):
            continue
        text = " ".join(str(entry.get("text") or "").split())
        if not text or len(text) > 240 or text.lower() in seen:
            continue
        seen.add(text.lower())
        label = " ".join(str(entry.get("label") or "").split())[:40]
        kind = str(entry.get("kind") or "").strip().lower()
        ideas.append({"text": text, "label": label, "kind": kind if kind in CHAT_IDEA_KINDS else ""})
        if len(ideas) == CHAT_IDEAS_MAX:
            break
    generated = raw.get("generated") if isinstance(raw, dict) else None
    return {"ideas": ideas, "generated": generated if isinstance(generated, str) and ideas else None}


@app.get("/api/chat-ideas")
async def api_chat_ideas():
    """Workspace-specific ideas for a new chat; an empty list means use the page's own."""
    return JSONResponse(load_chat_ideas(), headers={"Cache-Control": "no-store"})


@app.get("/runner")
async def runner_page():
    return FileResponse(page_file("index"))


def is_chat_id(item_id: str) -> bool:
    """Chats the user starts (a question, a document, a worker, a new worker), never board items."""
    item_id = str(item_id or "")
    return len(item_id) <= 520 and (
        is_general_chat_id(item_id)
        or is_hire_chat_id(item_id)
        or item_id.startswith((DOCUMENT_CHAT_PREFIX, WORKER_CHAT_PREFIX)))


def standing_thread_ids(raw: str) -> list[str]:
    """Chat ids the board page has open, so their history comes back with the board.

    Only the reserved namespaces are accepted, and the list is capped.
    """
    ids = []
    for part in str(raw or "").split(","):
        item_id = part.strip()
        if is_chat_id(item_id) and item_id not in ids:
            ids.append(item_id)
    return ids[:20]


def archived_chats() -> dict[str, str]:
    try:
        data = json.loads(ARCHIVED_CHATS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def chat_list(histories: dict, archived: dict[str, str], limit: int = 100) -> list[dict]:
    """The Chats tab: every chat the user started, newest activity first.

    Archiving hides a chat only until it has newer activity, so reopening and
    asking again brings it back without an unarchive step.
    shortcut: newest 100 only, page it if the history ever needs scrolling further back.
    """
    rows = []
    for item_id, history in histories.items():
        if not (history and is_chat_id(item_id)):
            continue
        at = history[-1].get("at", "")
        if item_id in archived and archived[item_id] >= at:
            continue
        opening = next((t for t in history if t.get("role") == "user"), {})
        first = opening.get("text") or next((a["name"] for a in opening.get("attachments", [])), "")
        rows.append({"id": item_id, "at": at, "preview": " ".join(first.split())[:140],
                     "turns": len(history)})
    rows.sort(key=lambda r: r["at"], reverse=True)
    return rows[:limit]


@app.get("/api/board")
async def api_board(threads: str = ""):
    """Cached conductor output for the rail + done-today, plus agent health.

    Handled items (AI-solved or user-closed) are filtered out of the rail
    immediately and surfaced as done_today, so a resolved item disappears
    without waiting for the next conductor pass.
    """
    state = runner.get_state("assistant")
    doc = normalise_board_doc((state or {}).get("last_output", {}).get("details", {}))
    states = runner.get_states()
    reminders = chat_router.reminders() if chat_router else []
    doc["items"], later_reminders = collect_board_items(doc["items"], states, reminders)
    handled = chat_router.handled() if chat_router else {}
    current_items = [i for i in doc["items"] if i["id"] not in handled]
    snoozed = chat_router.snoozed() if chat_router else {}
    doc["items"], doc["later"] = split_snoozed_items(current_items, snoozed)
    parked = {i["id"] for i in doc["later"]}
    doc["later"] = doc["later"] + [r for r in later_reminders
                                   if r["id"] not in parked and r["id"] not in handled]
    doc["later"].sort(key=lambda i: i.get("snoozed_until", ""))
    if chat_router:
        track_ids = [
            item["id"] for item in doc["items"] + doc["later"] if item.get("id")
        ]
        raised = chat_router.record_raised(track_ids)
        attach_raised_at(doc["items"], raised)
        attach_raised_at(doc["later"], raised)
    done = [{"text": v.get("result", ""), "at": v.get("at", "")}
            for v in sorted(handled.values(), key=lambda v: v.get("at", ""), reverse=True)]
    doc["done_today"] = done + doc.get("done_today", [])
    doc["closed"] = recently_closed(handled, chat_router)
    conversation_ids = [
        item["id"] for item in doc["items"] + doc["later"]
    ] + standing_thread_ids(threads)
    chat_histories = {
        item_id: history
        for item_id in conversation_ids
        if (history := chat_router.conversation_history(item_id) if chat_router else [])
    }
    return JSONResponse({
        "status": (state or {}).get("status", "idle"),
        "last_run": (state or {}).get("last_run", ""),
        "agent_up": agent.alive,
        "ai_backend": ai_backend.label(),
        "health": {**board_health(states), "restart_pending": restart_pending()},
        "active_chat_ids": chat_router.active_items() if chat_router else [],
        "chat_histories": chat_histories,
        "chats": chat_list(chat_router.conversation_histories(), archived_chats())
        if chat_router else [],
        "pending_approvals": chat_router.pending_approvals() if chat_router else {},
        "document": doc,
    })


@app.get("/api/board/count")
async def api_board_count():
    """Rail item count for the nav badge; mirrors api_board's filtering without its side effects."""
    state = runner.get_state("assistant")
    doc = normalise_board_doc((state or {}).get("last_output", {}).get("details", {}))
    reminders = chat_router.reminders() if chat_router else []
    items, _ = collect_board_items(doc["items"], runner.get_states(), reminders)
    handled = chat_router.handled() if chat_router else {}
    snoozed = chat_router.snoozed() if chat_router else {}
    items, later = split_snoozed_items([i for i in items if i["id"] not in handled], snoozed)
    today = datetime.now(timezone.utc).date().isoformat()
    # notes/later/done feed the Office whiteboard kanban
    notes = [{"title": i.get("title", ""), "owner": i.get("action_owner", "assistant"),
              "type": i.get("action_type", ""), "new": bool(i.get("new"))} for i in items]
    return JSONResponse({"items": len(items), "notes": notes, "later": len(later),
                         "done": sum(str(v.get("at", "")).startswith(today) for v in handled.values())})


@app.post("/api/board/refresh")
async def api_board_refresh():
    if not runner.trigger("assistant"):
        return JSONResponse({"error": "conductor task not configured"}, status_code=404)
    return JSONResponse({"triggered": True})


@app.post("/api/notify")
async def api_notify(body: dict):
    """Let a skill push a notification straight onto the board.

    Minimal shape: {title, body?, id?, url?, due?}. It becomes a board item with
    new=true, so the desktop notifier fires it (deduped by id) and the board
    shows it on its next poll. Closing it on the board removes it.

    With a `due` (ISO-8601, timezone-aware, in the future), it is instead saved
    as a durable, file-backed reminder that survives a restart and only surfaces
    when due - so a one-line curl can schedule a reminder, not just ping now.
    """
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid request"}, status_code=400)
    title = str(body.get("title", "")).strip()[:120]
    if not title:
        return JSONResponse({"error": "need title"}, status_code=400)
    if body.get("due") is not None:
        due = normalise_defer_until(body.get("due"))
        if due is None:
            return JSONResponse({"error": "invalid due time"}, status_code=400)
        if chat_router is None:
            return JSONResponse({"error": "reminders unavailable"}, status_code=503)
        reminder_id = chat_router.add_reminder(
            title, str(body.get("body", "")).strip(), due)
        return JSONResponse({"ok": True, "id": reminder_id, "due": due})
    item_id = str(body.get("id") or f"notify-{uuid.uuid4().hex[:8]}")
    url = str(body.get("url", "")).strip()
    links = [{"label": str(body.get("label", "Open"))[:40], "url": url}] \
        if url.startswith("http") else []
    item = {
        "id": item_id, "project_ref": None, "title": title,
        "why": clip_body(body.get("body", "")),
        "situation": str(body.get("situation", "")).strip(),
        "opening_move": str(body.get("opening_move", "Take a look")).strip()[:200],
        "action_owner": "user", "action_label": "Review",
        "action_type": str(body.get("action_type", "task"))[:20],
        "links": links, "new": True,
    }
    _pushed_notifications[:] = [n for n in _pushed_notifications if n["id"] != item_id]
    _pushed_notifications.append(item)
    return JSONResponse({"ok": True, "id": item_id})


def _current_board_item(item_id: str) -> dict | None:
    """Resolve one visible board item across all sources, or None."""
    state = runner.get_state("assistant")
    doc = normalise_board_doc((state or {}).get("last_output", {}).get("details", {}))
    active, later = collect_board_items(
        doc["items"], runner.get_states(),
        chat_router.reminders() if chat_router else [])
    return next((i for i in active + later if i["id"] == item_id), None)


@app.post("/api/board/close")
async def api_board_close(body: dict):
    """Explicitly close an item (the user's call). Drops it from the rail now and
    the conductor suppresses it next pass. A snapshot of the item is stored so
    it can be reviewed and revived from the Recently closed list."""
    if chat_router is None and not await ensure_chat_router():
        return chat_unavailable()
    if not isinstance(body, dict) or "item_id" not in body:
        return JSONResponse({"error": "need item_id"}, status_code=400)
    item_id = str(body["item_id"])
    snapshot = _current_board_item(item_id) or chat_router.known_item(item_id)
    chat_router.mark_handled(item_id, str(body.get("result", "Closed by " + profile.load().user)), snapshot)
    return JSONResponse({"closed": True})


@app.post("/api/board/reopen")
async def api_board_reopen(body: dict):
    """Revive a recently closed item. Drops it from the handled store and, using
    its stored snapshot, pushes it back onto the rail under its original id so the
    conversation thread is preserved. If the conductor still produces it, the
    un-handle alone suffices and the pushed copy is skipped as a duplicate."""
    if chat_router is None and not await ensure_chat_router():
        return chat_unavailable()
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid request"}, status_code=400)
    item_id = str(body.get("item_id", "")).strip()
    if not item_id or len(item_id) > 200:
        return JSONResponse({"error": "need valid item_id"}, status_code=400)
    if item_id not in chat_router.handled():
        return JSONResponse({"error": "item not closed"}, status_code=404)
    snapshot = chat_router.unhandle(item_id)
    if snapshot:
        item = {
            "id": item_id,
            "project_ref": _normalise_project_ref(snapshot.get("project_ref")),
            "title": str(snapshot.get("title", "")) or "Reopened item",
            "why": str(snapshot.get("why", "")),
            "situation": str(snapshot.get("situation", "")),
            "opening_move": str(snapshot.get("opening_move", "Take a look")),
            "action_type": _normalise_action_type(snapshot.get("action_type"), snapshot),
            "action_owner": _normalise_action_owner(snapshot.get("action_owner")),
            "action_label": _normalise_action_label(
                snapshot.get("action_label"),
                _normalise_action_type(snapshot.get("action_type"), snapshot)),
            "links": _normalise_links(snapshot.get("links")),
            "documents": _normalise_documents(snapshot.get("documents")),
            "new": True,
        }
        _pushed_notifications[:] = [n for n in _pushed_notifications if n["id"] != item_id]
        _pushed_notifications.append(item)
    return JSONResponse({"reopened": True, "restored": bool(snapshot)})


@app.post("/api/board/snooze")
async def api_board_snooze(body: dict):
    """Park a current item until an allowlisted future point."""
    if chat_router is None and not await ensure_chat_router():
        return chat_unavailable()
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid request"}, status_code=400)
    item_id = str(body.get("item_id", "")).strip()
    preset = str(body.get("preset", "")).strip()
    if not item_id or len(item_id) > 200:
        return JSONResponse({"error": "need valid item_id"}, status_code=400)
    try:
        until = snooze_deadline(preset)
    except ValueError:
        return JSONResponse({"error": "unknown snooze preset"}, status_code=400)
    state = runner.get_state("assistant")
    doc = normalise_board_doc((state or {}).get("last_output", {}).get("details", {}))
    current_items, _ = collect_board_items(
        doc["items"], runner.get_states(), chat_router.reminders())
    handled = chat_router.handled()
    current_items = [item for item in current_items if item["id"] not in handled]
    active_items, _ = split_snoozed_items(current_items, chat_router.snoozed())
    if not any(item["id"] == item_id for item in active_items):
        return JSONResponse({"error": "item not found"}, status_code=404)
    chat_router.snooze(item_id, until)
    return JSONResponse({"snoozed": True, "until": until})


@app.post("/api/board/unsnooze")
async def api_board_unsnooze(body: dict):
    """Bring a parked item back into the active queue now."""
    if chat_router is None and not await ensure_chat_router():
        return chat_unavailable()
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid request"}, status_code=400)
    item_id = str(body.get("item_id", "")).strip()
    if not item_id or len(item_id) > 200:
        return JSONResponse({"error": "need valid item_id"}, status_code=400)
    chat_router.unsnooze(item_id)
    return JSONResponse({"unsnoozed": True})


@app.post("/api/board/chat")
async def api_board_chat(body: dict):
    if chat_router is None and not await ensure_chat_router():
        return chat_unavailable()
    if not isinstance(body, dict) or "item_id" not in body:
        return JSONResponse({"error": "need item_id"}, status_code=400)
    item_id = str(body["item_id"])
    if item_id == AI_SETUP_ITEM_ID or (
        (setup_item() or {}).get("id") == AI_SETUP_ITEM_ID
        and (item_id == SETUP_ITEM_ID or not chat_router._sessions.get(item_id))
    ):
        return JSONResponse({"error": "Connect and check your AI in Settings before starting workspace setup or a new chat.",
                             "settings_url": "/settings#account-setup"}, status_code=503)
    if is_general_chat_id(item_id):
        item = {**GENERAL_CHAT_ITEM, "id": item_id}
    elif item_id.startswith(DOCUMENT_CHAT_PREFIX):
        # Likewise a document thread is opened from the viewer, not the rail.
        item = document_chat_item(item_id)
        if item is None:
            return JSONResponse({"error": "document not found"}, status_code=404)
    elif item_id.startswith(WORKER_CHAT_PREFIX):
        # A 1:1 with a worker, opened from its Office dossier or its Workers row.
        item = worker_chat_item(item_id, load_tasks_map(),
                                runner.get_state(item_id[len(WORKER_CHAT_PREFIX):]),
                                activity.recent(ActivityLog.MAX))
        if item is None:
            return JSONResponse({"error": "worker not found"}, status_code=404)
    elif item_id.startswith(HIRE_CHAT_PREFIX):
        # New worker on the Workers page: design a skill and put it on the schedule.
        item = hire_chat_item(item_id)
        if item is None:
            return JSONResponse({"error": "invalid chat id"}, status_code=404)
    else:
        state = runner.get_state("assistant")
        doc = normalise_board_doc((state or {}).get("last_output", {}).get("details", {}))
        active, later = collect_board_items(
            doc["items"], runner.get_states(), chat_router.reminders())
        item = next((i for i in active + later if i["id"] == item_id), None)
        if item is None:
            # The conductor drops items it considers resolved, capped out or aged
            # off, but the user may still be mid-conversation about one. Continue the
            # existing thread rather than 404ing a live chat off the board.
            item = chat_router.known_item(item_id)
        if item is None:
            return JSONResponse({"error": "item not found"}, status_code=404)

    if "context_paths" in body:
        try:
            documents = await asyncio.to_thread(context_documents, VAULT_ROOT, body["context_paths"])
        except (OSError, UnicodeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        item = {**item, "context_documents": documents}
    if "uploads" in body:
        try:
            files = await asyncio.to_thread(attachments.describe, UPLOADS_DIR, item_id, body["uploads"])
        except FileNotFoundError:
            return JSONResponse({"error": "Attachment not found. Add it again."}, status_code=400)
        except (OSError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if files:
            vault = VAULT_ROOT.resolve()
            item = {**item, "uploaded_files": [
                {**{key: file[key] for key in ("id", "name", "size", "image")},
                 "path": Path(file["path"]).relative_to(vault).as_posix()
                         if Path(file["path"]).is_relative_to(vault) else file["path"]}
                for file in files]}
    handle = chat_router.start_turn(item["id"], item, str(body.get("message", "")))
    if handle.queued:
        return JSONResponse({"queued": True, "position": handle.position}, status_code=202)

    events = handle.events
    if events is None:
        return JSONResponse({"error": "chat could not start"}, status_code=500)

    async def gen():
        while True:
            ev = await events.get()
            yield f"data: {json.dumps(ev)}\n\n"
            if ev.get("kind") == "_close":
                break

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/board/chat/upload", dependencies=[Depends(same_origin_upload)])
async def api_board_chat_upload(request: Request, item_id: str = "", name: str = ""):
    """Store one raw-body file for a conversation; the chat send references it by id."""
    if not item_id or len(item_id) > attachments.MAX_ITEM_ID:
        return JSONResponse({"error": "need valid item_id"}, status_code=400)
    too_big = JSONResponse({"error": "File exceeds the 20 MB upload limit."}, status_code=413)
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > attachments.MAX_UPLOAD_BYTES:
        return too_big
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > attachments.MAX_UPLOAD_BYTES:
            return too_big
    try:
        saved = await asyncio.to_thread(attachments.save, UPLOADS_DIR, item_id, name, bytes(data))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(saved)


@app.get("/api/board/chat/upload/{upload_id}")
async def api_board_chat_upload_get(upload_id: str, item_id: str = ""):
    """Serve an upload back: sniffed images inline, everything else as a download."""
    try:
        path = attachments.resolve(UPLOADS_DIR, item_id, upload_id)
    except ValueError:
        return JSONResponse({"error": "invalid attachment"}, status_code=400)
    except FileNotFoundError:
        return JSONResponse({"error": "attachment not found"}, status_code=404)
    with path.open("rb") as handle:
        kind = attachments.image_type(handle.read(16))
    headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"}
    if kind:
        return FileResponse(path, media_type=kind, headers=headers)
    return FileResponse(path, media_type="application/octet-stream",
                        filename=path.name[len(upload_id) + 1:], headers=headers)


@app.post("/api/chats/archive")
async def api_chats_archive(body: dict):
    """Hide a chat from the Chats tab. History is kept; new activity brings it back."""
    item_id = str((body or {}).get("item_id", "")).strip() if isinstance(body, dict) else ""
    if not is_chat_id(item_id):
        return JSONResponse({"error": "need a chat id"}, status_code=400)
    archived = archived_chats()
    archived[item_id] = datetime.now(timezone.utc).isoformat()
    ARCHIVED_CHATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVED_CHATS_FILE.write_text(json.dumps(archived, indent=2))
    return JSONResponse({"archived": True})


@app.post("/api/board/chat/{run_id}/approve")
async def api_board_approve(run_id: str, body: dict):
    if chat_router is None and not await ensure_chat_router():
        return chat_unavailable()
    approved = bool(body.get("approved")) if isinstance(body, dict) else False
    edit = body.get("edit") if isinstance(body, dict) else None
    result = await chat_router.approve(run_id, approved, edit)
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)


@app.get("/api/vault-document")
async def api_vault_document(path: str = ""):
    """Return one validated vault document (Markdown, or plain text as source)."""
    try:
        document = resolve_vault_text(path)
    except ValueError:
        return JSONResponse({"error": "invalid document path"}, status_code=400)
    except FileNotFoundError:
        return JSONResponse({"error": "document not found"}, status_code=404)
    if document.stat().st_size > 2 * 1024 * 1024:
        return JSONResponse({"error": "document is too large"}, status_code=413)
    try:
        content = document.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return JSONResponse({"error": "document is not UTF-8"}, status_code=415)
    relative = document.relative_to(VAULT_ROOT.resolve()).as_posix()
    if document.suffix.lower() in VAULT_SOURCE_SUFFIXES:
        return JSONResponse({
            "path": relative, "title": document.name,
            "content": source_as_markdown(content, document.suffix), "properties": [],
        })
    title = markdown_title(content, document.stem)
    body, properties = split_front_matter(content)
    return JSONResponse({
        "path": relative, "title": title, "content": body, "properties": properties,
    })


def split_front_matter(content: str) -> tuple[str, list[list[str]]]:
    """Separate YAML front matter from a Markdown body as display-ready pairs.

    Marked would otherwise render the block as a rule plus a setext heading.
    """
    match = re.match(r"\A---\s*\n(.*?)\n---[ \t]*(?:\n|\Z)", content, re.DOTALL)
    if not match:
        return content, []
    try:
        metadata = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return content, []
    if not isinstance(metadata, dict):
        return content, []
    properties = []
    for key, value in metadata.items():
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value if not isinstance(v, (dict, list)))
        elif isinstance(value, dict) or value is None:
            continue
        if str(value).strip():
            properties.append([str(key), str(value)])
    return content[match.end():].lstrip("\n"), properties


def changelog_version(text: str) -> str:
    """The version is the first `## x.y.z` heading in CHANGELOG.md."""
    match = re.search(r"^## (\d+\.\d+\.\d+)\b", text, re.M)
    return match.group(1) if match else "0.0.0"


@app.get("/api/changelog")
async def api_changelog():
    try:
        text = (DASHBOARD_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError:
        text = ""
    return {"version": changelog_version(text), "content": text}


NEWS_FEEDS = {"news": "https://feeds.bbci.co.uk/news/rss.xml", "sport": "https://feeds.bbci.co.uk/sport/rss.xml"}
NEWS_TTL = 900
_news_cache: dict = {"at": 0.0, "data": None}


def parse_rss(xml_bytes: bytes, limit: int = 10) -> list[dict]:
    """Headline + standfirst pairs from an RSS 2.0 feed."""
    import xml.etree.ElementTree as ET
    out = []
    for item in ET.fromstring(xml_bytes).iter("item"):
        title = (item.findtext("title") or "").strip()
        if title:
            out.append({"title": title, "summary": (item.findtext("description") or "").strip(),
                        "link": (item.findtext("link") or "").strip()})
        if len(out) >= limit:
            break
    return out


def lead_paragraphs(page_html: str, limit: int = 2) -> list[str]:
    """The opening paragraphs of a BBC article: the <p>s inside its text-block components."""
    import html as html_lib
    out = []
    for block in re.findall(r'data-component="text-block".*?</div>', page_html, re.S):
        for p in re.findall(r"<p\b[^>]*>(.*?)</p>", block, re.S):
            text = html_lib.unescape(re.sub(r"<[^>]+>", "", p)).strip()
            if text:
                out.append(text)
            if len(out) >= limit:
                return out
    return out


def _fetch(url: str, limit: int) -> bytes:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "office-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return resp.read(limit)


def _article_body(item: dict) -> None:
    try:
        item["body"] = lead_paragraphs(_fetch(item["link"], 2_000_000).decode("utf-8", "replace")) if item.get("link") else []
    except Exception:  # no body is fine: the page falls back to the RSS standfirst
        item["body"] = []


def fetch_news() -> dict:
    from concurrent.futures import ThreadPoolExecutor
    sections = {}
    for name, url in NEWS_FEEDS.items():
        try:
            sections[name] = parse_rss(_fetch(url, 1_000_000))
        except Exception as exc:  # a dead feed shows as "not in transmission", never breaks the page
            logging.getLogger(__name__).info("news feed %s failed: %s", name, exc)
            sections[name] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_article_body, [it for items in sections.values() for it in items]))
    return {"source": "BBC", "fetched": datetime.now(timezone.utc).isoformat(), "sections": sections}


@app.get("/api/news")
async def api_news():
    """Public BBC headlines for the Office lounge TV (Ceefax style), cached for 15 minutes."""
    if _news_cache["data"] is None or time.time() - _news_cache["at"] > NEWS_TTL:
        data = await asyncio.to_thread(fetch_news)
        if any(data["sections"].values()) or _news_cache["data"] is None:
            _news_cache.update(at=time.time(), data=data)
    return _news_cache["data"]


# Bamboozle! (the Teletext quiz) on the lounge TV: ten fresh questions a day, easiest first.
BAMBOOZLE_FILE = CACHE_DIR / "bamboozle.json"
BAMBOOZLE_SOURCES = (
    ("The Trivia API", "https://the-trivia-api.com/v2/questions?limit=10&region=GB"),
    ("Open Trivia DB", "https://opentdb.com/api.php?amount=10&type=multiple&encode=url3986"),
)
BAMBOOZLE_MIN = 5
_bamboozle_lock = asyncio.Lock()


def parse_quiz(source: str, payload) -> list[dict]:
    """Normalise either trivia API into {question, category, difficulty, correct, wrong}."""
    out = []
    if source == "Open Trivia DB":
        rows = (payload or {}).get("results") or []
        get = lambda r: (unquote(r.get("question") or ""), unquote(r.get("category") or ""), r.get("difficulty"),
                         unquote(r.get("correct_answer") or ""), [unquote(a) for a in r.get("incorrect_answers") or []])
    else:
        rows = payload if isinstance(payload, list) else []
        get = lambda r: (((r.get("question") or {}).get("text") or ""), (r.get("category") or "").replace("_", " "),
                         r.get("difficulty"), r.get("correctAnswer") or "", list(r.get("incorrectAnswers") or []))
    for row in rows:
        try:
            q, cat, diff, right, wrong = get(row)
        except (AttributeError, TypeError):
            continue
        q, right, wrong = str(q).strip(), str(right).strip(), [str(a).strip() for a in wrong][:3]
        if not q or not right or len(wrong) != 3 or len({right, *wrong}) != 4 or not all(wrong):
            continue  # every question needs four distinct answers, one per coloured key
        out.append({"question": q, "category": str(cat).strip().upper(), "difficulty": diff if diff in ("easy", "medium", "hard") else "medium",
                    "correct": right, "wrong": wrong})
    return out


def build_quiz(day: str, source: str, questions: list[dict]) -> dict:
    """Order easiest first and shuffle answers reproducibly for the day."""
    import random
    rank = {"easy": 0, "medium": 1, "hard": 2}
    rng = random.Random(day)
    out = []
    for q in sorted(questions, key=lambda q: rank[q["difficulty"]])[:10]:
        options = [q["correct"], *q["wrong"]]
        rng.shuffle(options)
        out.append({"question": q["question"], "category": q["category"], "difficulty": q["difficulty"],
                    "options": options, "answer": options.index(q["correct"])})
    return {"date": day, "source": source, "questions": out}


def fetch_quiz_questions() -> tuple[str, list[dict]] | None:
    import urllib.request
    for source, url in BAMBOOZLE_SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "office-dashboard/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                questions = parse_quiz(source, json.loads(resp.read(1_000_000)))
            if len(questions) >= BAMBOOZLE_MIN:
                return source, questions
        except Exception as exc:  # try the next source; no source means an off-air page, not an error
            logging.getLogger(__name__).info("quiz source %s failed: %s", source, exc)
    return None


def daily_quiz(day: str, cache_file: Path = BAMBOOZLE_FILE, fetch=fetch_quiz_questions) -> dict | None:
    """Today's quiz, fetched once a day; yesterday's set (marked stale) if every source is down."""
    try:
        cached = json.loads(cache_file.read_text())
    except (OSError, ValueError):
        cached = None
    if cached and cached.get("date") == day:
        return cached
    got = fetch()
    if got:
        quiz = build_quiz(day, *got)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(quiz))
        return quiz
    return {**cached, "stale": True} if cached else None


@app.get("/api/bamboozle")
async def api_bamboozle():
    """Today's Bamboozle! quiz for the Office lounge TV."""
    async with _bamboozle_lock:  # one fetch per day even if several tabs ask at once
        quiz = await asyncio.to_thread(daily_quiz, datetime.now().date().isoformat())
    if not quiz:
        return JSONResponse({"error": "not in transmission"}, status_code=503)
    return quiz


@app.get("/changelog")
async def changelog_page():
    return FileResponse(page_file("changelog"))


@app.get("/settings")
async def settings_page():
    return FileResponse(page_file("settings"))


@app.get("/document")
async def document_page():
    return FileResponse(page_file("document"))


@app.get("/workspace")
async def workspace_page():
    return FileResponse(page_file("workspace"))


@app.get("/api/workspace")
async def api_workspace():
    return JSONResponse(await asyncio.to_thread(build_catalogue, VAULT_ROOT),
                        headers={"Cache-Control": "no-store"})


def file_browser_command(folder: Path) -> list[str] | None:
    if sys.platform == "darwin":
        return ["open", str(folder)]
    if sys.platform == "win32":
        return ["explorer", str(folder)]
    return ["xdg-open", str(folder)] if shutil.which("xdg-open") else None


def launch_detached(command: list[str]) -> None:
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


@app.post("/api/workspace/reveal", dependencies=[Depends(local_ai_settings)])
async def api_workspace_reveal():
    """Open the active workspace in this computer's file browser."""
    command = file_browser_command(paths.WORKSPACE)
    if not command:
        return JSONResponse({"error": "No file browser is available on this computer."}, 400)
    # SECURITY-REVIEW: fixed binary and the runner's own workspace path; no request input.
    try:
        launch_detached(command)
    except OSError:
        return JSONResponse({"error": "Roosta could not open the workspace folder."}, 500)
    return {"opened": str(paths.WORKSPACE)}


@app.get("/assistant")
async def assistant_page():
    return FileResponse(page_file("assistant"))


@app.get("/api/pulse")
async def api_pulse():
    """Live vault telemetry, AI activity and board mood for the Pulse page."""
    states = runner.get_states()
    health = board_health(states)
    handled = chat_router.handled() if chat_router else {}
    state = runner.get_state("assistant")
    doc = normalise_board_doc((state or {}).get("last_output", {}).get("details", {}))
    doc["items"] = merge_board_feed_items(doc["items"], states, board_merges(states))
    doc["items"] = merge_pushed_notifications(doc["items"])
    reminders = chat_router.reminders() if chat_router else []
    due_reminders, later_reminders = split_due_reminders(reminders)
    have = {item["id"] for item in doc["items"]}
    doc["items"] = [item for item in due_reminders if item["id"] not in have] + doc["items"]
    current_items = [item for item in doc["items"] if item["id"] not in handled]
    snoozed = chat_router.snoozed() if chat_router else {}
    active_items, later_items = split_snoozed_items(current_items, snoozed)
    parked = {item["id"] for item in later_items}
    later_items = later_items + [
        item for item in later_reminders
        if item["id"] not in parked and item["id"] not in handled
    ]
    task_states = runner.get_states()
    chat_items = []
    if chat_router:
        for item_id in chat_router.active_items():
            known = chat_router.known_item(item_id)
            chat_items.append(known or {"id": item_id, "title": "Conversation"})
    return JSONResponse(build_pulse(
        VAULT_ROOT,
        activity.recent(200),
        handled,
        len(active_items),
        len(later_items),
        health,
        task_states=task_states,
        running_activity=activity.running(),
        chat_items=chat_items,
        active_project_items=[
            {"id": item["id"], "title": item.get("title", ""), "project_ref": item["project_ref"]}
            for item in active_items if item.get("project_ref")
        ],
    ))


@app.get("/api/constellation/{slug}")
async def api_constellation_project(slug: str):
    """Briefing for one project star: README excerpts, provenance, links, recent files."""
    try:
        return JSONResponse(await asyncio.to_thread(project_brief, VAULT_ROOT, slug),
                            headers={"Cache-Control": "no-store"})
    except ValueError:
        return JSONResponse({"error": "invalid project"}, status_code=400)
    except FileNotFoundError:
        return JSONResponse({"error": "project not found"}, status_code=404)


@app.post("/api/constellation/{slug}/review")
async def api_constellation_review(slug: str, body: dict):
    """Weekly-review decision: change status, or record the user's confirmation.

    A local vault write the user makes by clicking; it touches only the README
    frontmatter keys `status`, `last_updated`, `verified` and `stale_after`.
    """
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid request"}, status_code=400)
    try:
        brief = await asyncio.to_thread(
            review_project, VAULT_ROOT, slug,
            str(body.get("action", "")), str(body.get("status", "")),
            reviewer=profile.load().reviewer,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except FileNotFoundError:
        return JSONResponse({"error": "project not found"}, status_code=404)
    return JSONResponse(brief)


@app.get("/office")
async def office_page():
    return FileResponse(page_file("office"))


@app.get("/pulse")
async def pulse_page():
    return FileResponse(page_file("pulse"))


@app.get("/constellation")
async def constellation_page():
    # Same page in full-screen mode; pulse.html switches layout on the path.
    return FileResponse(page_file("pulse"))


# Mount static after routes so / doesn't get overridden
static_dir = DASHBOARD_ROOT / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


if __name__ == "__main__":
    from roosta.cli import main

    raise SystemExit(main(["start"]))
