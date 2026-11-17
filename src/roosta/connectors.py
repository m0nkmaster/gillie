"""Connectors: tool sign-in checks and write denials, declared in `.roosta/connectors.yaml`.

The runner knows no specific tool. A connector may name a read-only `check` command and how to
read its result; a task that lists it in `requires: [<id>]` runs that check just before it starts
and waits as `blocked`, with the sign-in command, while the tool is signed out. A connector's
`deny` list adds its write commands to the safety policy every unattended run gets. Example:

    connectors:
      - id: github
        name: GitHub
        check: gh auth status          # exit 0 means signed in
        login: gh auth login
        deny: [gh pr create, gh issue create]

With JSON output, `expect` and `reject` read dotted fields instead of the exit code.

Verdicts: `success`; `auth-failed` (the check ran and says signed out); `error` (the check could
not run or its output made no sense). An `error` is not a sign-in verdict, so it never blocks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
DEFAULT_TIMEOUT = 45
# Tasks that start together share one check rather than each running it.
CHECK_CACHE_SECONDS = 60


@dataclass(frozen=True)
class Connector:
    id: str
    check: list[str] = field(default_factory=list)
    name: str = ""
    expect: dict = field(default_factory=dict)
    reject: dict = field(default_factory=dict)
    login: str = ""
    timeout: int = DEFAULT_TIMEOUT
    deny: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.name or self.id


def _command(raw) -> list[str]:
    if isinstance(raw, str):
        try:
            return shlex.split(raw)
        except ValueError:
            return []
    if isinstance(raw, list) and all(isinstance(part, (str, int, float)) for part in raw):
        return [str(part) for part in raw]
    return []


def parse_connectors(data) -> list[Connector]:
    """Validate parsed YAML into connectors, skipping (and logging) malformed entries."""
    raw = data.get("connectors") if isinstance(data, dict) else None
    connectors: list[Connector] = []
    seen: set[str] = set()
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        cid = str(entry.get("id", "")).strip().lower()
        check = _command(entry.get("check"))
        if not ID_RE.fullmatch(cid) or cid in seen or ("check" in entry and not check):
            log.warning("Skipping connector %r: needs a unique id and a valid check command", cid)
            continue
        timeout = entry.get("timeout", DEFAULT_TIMEOUT)
        timeout = max(1, timeout) if isinstance(timeout, int) and not isinstance(timeout, bool) else DEFAULT_TIMEOUT

        seen.add(cid)
        connectors.append(Connector(
            id=cid,
            check=check,
            name=str(entry.get("name") or "").strip(),
            expect=entry["expect"] if isinstance(entry.get("expect"), dict) else {},
            reject=entry["reject"] if isinstance(entry.get("reject"), dict) else {},
            login=str(entry.get("login") or "").strip(),
            timeout=timeout,
            deny=[" ".join(cmd) for cmd in map(_command, entry.get("deny") or []) if cmd]
            if isinstance(entry.get("deny"), list) else [],
        ))
    return connectors


def load_connectors(path: Path) -> list[Connector]:
    try:
        return parse_connectors(yaml.safe_load(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return []
    except (OSError, yaml.YAMLError) as exc:
        log.warning("Could not read %s: %s", path, exc)
        return []


def shell_denials(path: Path) -> list[str]:
    """Every connector's write commands, as space-separated command prefixes."""
    return sorted({rule for c in load_connectors(path) for rule in c.deny})


def cursor_shell_rule(prefix: str) -> str:
    """`msteams teams_send_message` -> Cursor `Shell(msteams:teams_send_message*)`."""
    command, _, rest = prefix.partition(" ")
    return f"Shell({command}:{rest}*)" if rest else f"Shell({command})"


def _lookup(payload, dotted: str):
    value = payload
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


_MISSING = object()


def verdict(connector: Connector, returncode: int, stdout: str) -> tuple[str, str]:
    """Pure: read one check's result as (status, message).

    Without `expect`/`reject` the exit code decides. With them the output must be JSON, and
    every `expect` path must hold its value while no `reject` path does.
    """
    login = f" - run `{connector.login}`" if connector.login else ""
    down = f"{connector.label} not connected{login}."
    if not connector.expect and not connector.reject:
        return ("success", f"{connector.label} connected.") if returncode == 0 else ("auth-failed", down)
    try:
        payload = json.loads(stdout)
    except ValueError:
        return "error", f"could not parse {connector.check[0]} output"
    if not isinstance(payload, dict):
        return "error", f"unrecognised {connector.check[0]} output"
    if connector.expect and all(_lookup(payload, key) is _MISSING for key in connector.expect):
        return "error", f"unrecognised {connector.check[0]} output"
    failed = [key for key, want in connector.expect.items() if _lookup(payload, key) != want]
    failed += [key for key, bad in connector.reject.items() if _lookup(payload, key) == bad]
    if failed:
        return "auth-failed", f"{down} ({', '.join(failed)})"
    return "success", f"{connector.label} connected."


async def run_command(argv: list[str], timeout: float) -> tuple[int | None, str, str | None]:
    """Run one trusted command without a shell. Returns (returncode, stdout, error)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None, "", f"'{argv[0]}' not found on PATH"
    except OSError as exc:
        return None, "", f"could not launch {argv[0]}: {exc}"
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        # A CLI can exit while a helper it spawned still holds stdout open.
        if proc.returncode is None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=1)
            except (ProcessLookupError, asyncio.TimeoutError):
                pass
        return None, "", f"{argv[0]} did not finish within {timeout:g}s"
    return proc.returncode, raw.decode(errors="replace"), None


async def check_connector(connector: Connector, runner=run_command) -> tuple[str, str]:
    """Run a connector's check once and read the verdict."""
    code, out, error = await runner(connector.check, connector.timeout)
    if error:
        return "error", error
    return verdict(connector, code or 0, out)


class Connectors:
    """The workspace's connectors. The runner asks which of a task's are signed out."""

    def __init__(self, connectors: list[Connector] | None = None, *, path: Path | None = None,
                 checker=check_connector, clock=time.monotonic) -> None:
        self.by_id = {c.id: c for c in connectors or []}
        self._path = path
        self._mtime: float | None = None
        self._checker = checker
        self._clock = clock
        self._cache: dict[str, tuple[float, str, str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def refresh(self) -> None:
        """Re-read connectors.yaml when it changes, so setup can add one without a restart."""
        if self._path is None:
            return
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._mtime:
            self._mtime = mtime
            self.by_id = {c.id: c for c in load_connectors(self._path)}
            self._cache.clear()

    async def verdict(self, cid: str) -> tuple[str, str]:
        connector = self.by_id[cid]
        lock = self._locks.setdefault(cid, asyncio.Lock())
        async with lock:
            cached = self._cache.get(cid)
            if cached and self._clock() - cached[0] < CHECK_CACHE_SECONDS:
                return cached[1], cached[2]
            try:
                status, message = await self._checker(connector)
            except Exception as exc:  # a broken check must never take the runner down
                status, message = "error", f"{connector.label} check crashed: {exc}"
            self._cache[cid] = (self._clock(), status, message)
            return status, message

    async def signed_out(self, requires: list[str]) -> list[str]:
        """Labels of required connectors whose check says signed out, with how to sign in."""
        self.refresh()
        out = []
        for cid in requires:
            connector = self.by_id.get(cid)
            if connector is None or not connector.check:
                continue
            status, _ = await self.verdict(cid)
            if status == "auth-failed":
                out.append(f"{connector.label} (run `{connector.login}`)" if connector.login else connector.label)
        return out
