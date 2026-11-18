"""Skill Runner - periodic unattended AI harness execution with status UI."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, nullcontext
from dataclasses import asdict, replace
from datetime import datetime, timezone, timedelta
from pathlib import Path

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
                  is_general_chat_id, normalise_defer_until)
from roosta import ai_backend
from roosta import ai_connections, ai_runtime, ai_settings, github_issues, pages, pi_agent, teletext
from roosta.activity_log import ActivityLog
from roosta.claude_code import (
    CLAUDE_SIGNIN_MESSAGE,
    claude_args,
    claude_env,
    claude_result_text,
    claude_stream_has_progress,
    is_claude_auth_failure,
    is_transient_claude_failure,
    read_claude_transcript,
    session_exists as claude_session_exists,
)
from roosta.cursor_agent import (
    CURSOR_AGENT,
    GIT_PUSH_CONFIG_DIR,
    SAFE_CONFIG_DIR,
    cursor_args,
    cursor_env,
    cursor_result_text,
    cursor_stream_has_progress,
    is_transient_agent_failure,
)
from roosta.cursor_sessions import read_cursor_transcript, valid_session_id
from roosta.opencode_ollama import (
    is_unexecuted_tool_call,
    opencode_ollama_args,
    opencode_ollama_env,
    opencode_result_text,
)
from roosta.board import (
    AI_SETUP_ITEM_ID,
    SETUP_ITEM_ID,
    attach_raised_at,
    board_health,
    board_merges,
    carry_forward_board_items,
    collect_board_items,
    merge_board_feed_items,
    merge_pushed_notifications,
    normalise_board_doc,
    push_notification,
    recently_closed,
    restart_pending,
    setup_gaps,
    setup_item,
    snooze_deadline,
    split_due_reminders,
    split_snoozed_items,
)
from roosta.chat_items import (
    DOCUMENT_CHAT_PREFIX,
    GENERAL_CHAT_ITEM,
    HIRE_CHAT_PREFIX,
    WORKER_CHAT_PREFIX,
    chat_list,
    document_chat_item,
    hire_chat_item,
    is_chat_id,
    standing_thread_ids,
    worker_chat_item,
)
from roosta.ai_settings import (available_runtime_options, check_task_runtime, model_efforts, provider_models,
                                runtime_unavailable_message, validate_model)
from roosta.workspace_settings import captured, list_folders, skills_lock, workspace_managed_by, workspace_target
from roosta.task_config import (
    CLAUDE_HARNESS,
    CURSOR_HARNESS,
    OPENCODE_OLLAMA_HARNESS,
    TASK_SETTING_FLAGS,
    TaskConfig,
    TaskState,
    connection_task_model,
    load_tasks,
    load_tasks_map,
    remove_task,
    runtime_model_key,
    skill_description,
    skill_files,
    task_engine,
    task_settings_changes,
    update_task_fields,
    update_task_runtime,
    valid_runtime,
)
from roosta.pulse import build_pulse, markdown_title, project_brief, review_project
from roosta.usage import UsageStore
from roosta.workspace import (VAULT_SOURCE_SUFFIXES, build_catalogue, context_documents,
                              read_document, resolve_vault_markdown, resolve_vault_text,
                              source_as_markdown)
from roosta.workspace import metadata as workspace_metadata
from roosta.workspace import visible_path as workspace_visible

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

DASHBOARD_ROOT = paths.APP_ROOT
CACHE_DIR = paths.CACHE_DIR
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


def outcome_summary(output: dict, error: str) -> str:
    """One brief outcome line for the activity feed."""
    summary = output.get("summary") if isinstance(output, dict) else ""
    return str(summary or error or "")


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
                    await ai_backend.kill(proc)
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
                    await ai_backend.kill(proc)
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
            await ai_backend.kill(proc)
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
        tasks_file: Path = paths.TASKS_FILE,
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
                state.skill, next((t.dir for t in self._tasks if t.id == task_id), paths.WORKSPACE),
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
            str(paths.WORKSPACE),  # chats run at the workspace root
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
    activity.compact()  # before runner.start(), so no spawn is appending
    prune_cursor_session_stores()    # reclaim unread Cursor CLI scratch, same timing
    try:
        await asyncio.to_thread(ai_connections.offer_system)
    except (ValueError, OSError):
        log.warning("Could not detect an existing Claude Code account; open AI settings to continue")
    runner.start()
    await _start_chat_router()
    yield
    jobs = [task for task in ai_settings.job_tasks.values() if not task.done()]
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
        onboarding = setup_item(chat_router)
        missing = setup_gaps() if (onboarding and onboarding["id"] == SETUP_ITEM_ID
                                   and SETUP_ITEM_ID not in (chat_router.handled() if chat_router else {})) else []
        return ai_settings_result({**data, "providers": ai_connections.PROVIDERS, "runtimes": runtimes,
            "engines": engines, "terminal_available": sys.platform == "darwin",
            "workspace_setup_ready": bool(missing), "workspace_setup_missing": missing,
            "jobs": ai_settings.jobs, "selected": selected, "selected_model": selected_model,
            "legacy": ai_backend.current(),
            "affected": {c["id"]: connection_affected(c["id"]) for c in data["connections"]}})
    except ValueError as exc:
        return ai_settings_result({"error": str(exc)}, 400)


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
                and ai_connections.get(other)["provider"] == "openai" for other, job in ai_settings.jobs.items()
            ):
                raise ValueError("Finish the other OpenAI sign-in before starting this one")
        else:
            raise ValueError("Unknown operation")
        if key in ai_settings.job_tasks and not ai_settings.job_tasks[key].done():
            return ai_settings_result({"error": "This operation is already running"}, 409)
        if operation == "install":
            ai_runtime.INSTALLING.add(key)
        elif operation == "login":
            ai_connections.update(key, tested=False)
        ai_settings.jobs[key] = {"status": "running", "operation": operation}
        ai_settings.job_tasks[key] = asyncio.create_task(ai_settings.run_operation(key, operation, ensure_chat_router))
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
    result = await ai_settings.command_output("/usr/bin/osascript", "-e", script, command)
    if result.strip() != "opened":
        return ai_settings_result({"error": "Could not open Terminal; check macOS automation permissions and try again"}, 400)
    return ai_settings_result({"ok": True})


@app.post("/api/ai/connections/{connection_id}/change", dependencies=[Depends(local_ai_settings)])
async def api_ai_change(connection_id: str, body: dict):
    affected = connection_affected(connection_id)
    if (ai_backend.ACTIVE_CONNECTIONS[connection_id] or affected["pending_approvals"] or
            ai_settings.jobs.get(connection_id, {}).get("status") == "running"):
        return ai_settings_result({"error": "Finish current turns, setup operations and pending approvals before changing this connection"}, 409)
    try:
        return ai_settings_result(ai_connections.change(connection_id, body))
    except ValueError as exc:
        return ai_settings_result({"error": str(exc)}, 400)
    except Exception:
        return ai_settings_result({"error": "Could not update the OS credential store; unlock it and retry"}, 400)


@app.post("/api/ai/connections/{connection_id}/disconnect", dependencies=[Depends(local_ai_settings)])
async def api_ai_disconnect(connection_id: str):
    if ai_backend.ACTIVE_CONNECTIONS[connection_id] or ai_settings.jobs.get(connection_id, {}).get("status") == "running":
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
        resume = f"cd {shlex.quote(str(paths.WORKSPACE))} && claude --resume {session_id}"
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


# --- Custom pages (see pages.py) ---


def vault_files(pattern: str, root: Path | None = None) -> list[dict]:
    """Workspace Markdown matching a glob, with titles and frontmatter but no bodies."""
    root = (root or paths.WORKSPACE).resolve()
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
        if len(out) >= pages.PAGE_FILES_LIMIT:
            break
    return out


@app.get("/api/pages")
async def api_pages():
    return JSONResponse(pages.custom_pages())


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
    if not pages.PAGE_NAME.fullmatch(name) or not file.endswith(".json"):
        return JSONResponse({"error": "not found"}, status_code=404)
    target = pages.contained(pages.PAGE_DATA_DIR / name, file)
    if target is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(target, media_type="application/json", headers={"Cache-Control": "no-store"})


@app.get("/pages/{name}/{asset:path}")
async def custom_page(name: str, asset: str = ""):
    if not pages.PAGE_NAME.fullmatch(name):
        return JSONResponse({"error": "not found"}, status_code=404)
    target = pages.contained(paths.PAGES_DIR / name, asset or "index.html")
    if target is None or target.suffix.lower() not in pages.PAGE_TYPES:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(target, media_type=pages.PAGE_TYPES[target.suffix.lower()],
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
    return FileResponse(pages.page_file("index"))


def archived_chats() -> dict[str, str]:
    try:
        data = json.loads(ARCHIVED_CHATS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


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
    doc["items"], later_reminders = collect_board_items(doc["items"], states, reminders, chat_router)
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
    items, _ = collect_board_items(doc["items"], runner.get_states(), reminders, chat_router)
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
    push_notification(item)
    return JSONResponse({"ok": True, "id": item_id})


def _current_board_item(item_id: str) -> dict | None:
    """Resolve one visible board item across all sources, or None."""
    state = runner.get_state("assistant")
    doc = normalise_board_doc((state or {}).get("last_output", {}).get("details", {}))
    active, later = collect_board_items(
        doc["items"], runner.get_states(),
        chat_router.reminders() if chat_router else [], chat_router)
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
        item = normalise_board_doc({"items": [{**snapshot, "id": item_id}]})["items"][0]
        item["title"] = item["title"] or "Reopened item"
        item["opening_move"] = item["opening_move"] or "Take a look"
        item["new"] = True
        push_notification(item)
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
        doc["items"], runner.get_states(), chat_router.reminders(), chat_router)
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
        (setup_item(chat_router) or {}).get("id") == AI_SETUP_ITEM_ID
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
            doc["items"], runner.get_states(), chat_router.reminders(), chat_router)
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
            documents = await asyncio.to_thread(context_documents, paths.WORKSPACE, body["context_paths"])
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
            vault = paths.WORKSPACE.resolve()
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
    relative = document.relative_to(paths.WORKSPACE.resolve()).as_posix()
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


_news_cache: dict = {"at": 0.0, "data": None}
_bamboozle_lock = asyncio.Lock()


@app.get("/api/news")
async def api_news():
    """Public BBC headlines for the Office lounge TV (Ceefax style), cached for 15 minutes."""
    if _news_cache["data"] is None or time.time() - _news_cache["at"] > teletext.NEWS_TTL:
        data = await asyncio.to_thread(teletext.fetch_news)
        if any(data["sections"].values()) or _news_cache["data"] is None:
            _news_cache.update(at=time.time(), data=data)
    return _news_cache["data"]


@app.get("/api/bamboozle")
async def api_bamboozle():
    """Today's Bamboozle! quiz for the Office lounge TV."""
    async with _bamboozle_lock:  # one fetch per day even if several tabs ask at once
        quiz = await asyncio.to_thread(teletext.daily_quiz, datetime.now().date().isoformat())
    if not quiz:
        return JSONResponse({"error": "not in transmission"}, status_code=503)
    return quiz


@app.get("/changelog")
async def changelog_page():
    return FileResponse(pages.page_file("changelog"))


@app.get("/settings")
async def settings_page():
    return FileResponse(pages.page_file("settings"))


@app.get("/document")
async def document_page():
    return FileResponse(pages.page_file("document"))


@app.get("/workspace")
async def workspace_page():
    return FileResponse(pages.page_file("workspace"))


@app.get("/api/workspace")
async def api_workspace():
    return JSONResponse(await asyncio.to_thread(build_catalogue, paths.WORKSPACE),
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
    return FileResponse(pages.page_file("assistant"))


@app.get("/api/pulse")
async def api_pulse():
    """Live vault telemetry, AI activity and board mood for the Pulse page."""
    states = runner.get_states()
    health = board_health(states)
    handled = chat_router.handled() if chat_router else {}
    state = runner.get_state("assistant")
    doc = normalise_board_doc((state or {}).get("last_output", {}).get("details", {}))
    doc["items"] = merge_board_feed_items(doc["items"], states, board_merges(states, chat_router))
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
        paths.WORKSPACE,
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
        return JSONResponse(await asyncio.to_thread(project_brief, paths.WORKSPACE, slug),
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
            review_project, paths.WORKSPACE, slug,
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
    return FileResponse(pages.page_file("office"))


@app.get("/pulse")
async def pulse_page():
    return FileResponse(pages.page_file("pulse"))


@app.get("/constellation")
async def constellation_page():
    # Same page in full-screen mode; pulse.html switches layout on the path.
    return FileResponse(pages.page_file("pulse"))


# Mount static after routes so / doesn't get overridden
static_dir = DASHBOARD_ROOT / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


if __name__ == "__main__":
    from roosta.cli import main

    raise SystemExit(main(["start"]))
