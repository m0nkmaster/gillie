"""Cursor Agent CLI integration for dashboard tasks and conversations."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path

from roosta import connectors, paths

VAULT = str(paths.WORKSPACE)
CURSOR_ENV_FILE = Path(VAULT) / ".env"
CURSOR_AGENT = shutil.which("agent") or str(Path.home() / ".local" / "bin" / "agent")
SAFE_CONFIG_TEMPLATE = Path(__file__).resolve().parent / "cursor-permissions.json"
SAFE_CONFIG_DIR = paths.CACHE_DIR / "cursor-agent-config"
GIT_PUSH_CONFIG_TEMPLATE = Path(__file__).resolve().parent / "cursor-permissions-git-push.json"
GIT_PUSH_CONFIG_DIR = paths.CACHE_DIR / "cursor-agent-git-push-config"
CURSOR_MODEL_ENV = "CURSOR_MODEL"
GENERAL_CHAT_MODEL_ENV = "GENERAL_CHAT_MODEL"
CURSOR_MCP_SOURCE = Path.home() / ".cursor" / "mcp.json"
TRANSIENT_AGENT_FAILURES = (
    "getaddrinfo enotfound api2.cursor.sh",
    "ping timed out",
    "agent turn stopped after repeated resume attempts made no progress",
)


def is_transient_agent_failure(output: str) -> bool:
    """True only for Cursor transport failures known to mean no model progress."""
    lowered = output.lower()
    return any(marker in lowered for marker in TRANSIENT_AGENT_FAILURES)


def cursor_stream_has_progress(stdout: str) -> bool:
    """True once a Cursor stream contains model or tool activity.

    Cursor writes an init event and echoes the user prompt before the model
    starts. Those protocol events are safe to replay after a transient
    transport failure; every other event or malformed line is treated
    conservatively as progress.
    """
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return True
        if event.get("type") == "system" and event.get("subtype") == "init":
            continue
        if event.get("type") == "user":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if (
                isinstance(message, dict)
                and message.get("role") == "user"
                and isinstance(content, list)
                and all(
                    isinstance(part, dict) and part.get("type") == "text"
                    for part in content
                )
            ):
                continue
        return True
    return False


def agent_path(base_path: str | None = None) -> str:
    """PATH with common local bins prepended."""
    home = Path.home()
    preferred = [
        str(home / ".bun" / "bin"),
        str(home / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    inherited = [part for part in str(base_path or os.environ.get("PATH", "")).split(os.pathsep) if part]
    return os.pathsep.join(dict.fromkeys(preferred + inherited))


def ensure_mcp_config(config_dir: Path) -> None:
    """Mirror the user's Cursor MCP config into the dashboard agent config dir."""
    source = CURSOR_MCP_SOURCE
    if not source.is_file():
        return
    destination = Path(config_dir) / "mcp.json"
    try:
        if destination.is_file() and destination.stat().st_mtime >= source.stat().st_mtime:
            return
    except OSError:
        pass
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    destination.chmod(0o600)


def _dotenv_value(path: Path, key: str) -> str:
    """Read one simple dotenv value without evaluating the file as shell code."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return ""
    prefix = f"{key}="
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        value = stripped[len(prefix):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value
    return ""


def cursor_model(
    base: dict | None = None,
    env_file: Path = CURSOR_ENV_FILE,
) -> str:
    """Resolve the configured model from the process or workspace dotenv."""
    env = os.environ if base is None else base
    model = str(env.get(CURSOR_MODEL_ENV, "") or _dotenv_value(env_file, CURSOR_MODEL_ENV)).strip()
    return model if re.fullmatch(r"[A-Za-z0-9._-]{1,100}", model) else ""


def general_chat_model(
    base: dict | None = None,
    env_file: Path = CURSOR_ENV_FILE,
) -> str:
    """Resolve General chat's model without changing item chats or tasks."""
    env = os.environ if base is None else base
    model = str(
        env.get(GENERAL_CHAT_MODEL_ENV, "")
        or _dotenv_value(env_file, GENERAL_CHAT_MODEL_ENV)
    ).strip()
    return model if re.fullmatch(r"[A-Za-z0-9._-]{1,100}", model) else ""


# Login/token state the CLI caches into its config after an interactive sign-in.
# We strip these so a headless run can never fall back to a (possibly locked)
# macOS keychain login: the dashboard must authenticate purely via CURSOR_API_KEY.
CONFIG_LOGIN_KEYS = ("authInfo", "authToken", "authId", "accessToken", "refreshToken")


def merged_permissions(permissions: dict) -> dict:
    """The shipped policy plus each workspace connector's denied write commands."""
    extra = [connectors.cursor_shell_rule(rule)
             for rule in connectors.shell_denials(paths.CONNECTORS_FILE)]
    deny = list(permissions.get("deny", []))
    return {**permissions, "deny": deny + [rule for rule in extra if rule not in deny]}


def ensure_safe_config(
    config_dir: Path | None = None,
    template_path: Path | None = None,
) -> Path:
    """Seed the gitignored Cursor config while preserving CLI-managed state."""
    directory = Path(config_dir or SAFE_CONFIG_DIR)
    template = json.loads(Path(template_path or SAFE_CONFIG_TEMPLATE).read_text())
    template["permissions"] = merged_permissions(template["permissions"])
    path = directory / "cli-config.json"
    try:
        current = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        current = {}
    has_login_state = isinstance(current, dict) and any(
        k in current for k in CONFIG_LOGIN_KEYS
    )
    if (
        isinstance(current, dict)
        and not has_login_state
        and current.get("version") == template["version"]
        and current.get("permissions") == template["permissions"]
    ):
        return directory
    config = current if isinstance(current, dict) else {}
    for key in CONFIG_LOGIN_KEYS:
        config.pop(key, None)
    config["version"] = template["version"]
    config["editor"] = template["editor"]
    config["permissions"] = template["permissions"]
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = directory / f"cli-config.{os.getpid()}.tmp"
    temporary.write_text(json.dumps(config, indent=2))
    temporary.chmod(0o600)
    temporary.replace(path)
    ensure_mcp_config(directory)
    return directory


def cursor_env(
    base: dict | None = None,
    env_file: Path = CURSOR_ENV_FILE,
    *,
    safe_config: bool = True,
    allow_git_push: bool = False,
) -> dict:
    """Return a child environment with API auth and dashboard permissions.

    The workspace `.env` is authoritative for `CURSOR_API_KEY`: a stale or blank
    value inherited from the launching process (e.g. Roosta.app) is overridden
    so the agent always authenticates via the key and never falls back to a
    keychain login. The inherited value is used only when `.env` provides none.
    """
    env = dict(os.environ if base is None else base)
    env["PATH"] = agent_path(env.get("PATH"))
    key = _dotenv_value(Path(env_file), "CURSOR_API_KEY")
    if key:
        env["CURSOR_API_KEY"] = key
    if safe_config:
        if allow_git_push:
            env["CURSOR_CONFIG_DIR"] = str(
                ensure_safe_config(GIT_PUSH_CONFIG_DIR, GIT_PUSH_CONFIG_TEMPLATE)
            )
        else:
            env["CURSOR_CONFIG_DIR"] = str(ensure_safe_config())
    elif env.get("CURSOR_CONFIG_DIR") in {
        str(SAFE_CONFIG_DIR),
        str(GIT_PUSH_CONFIG_DIR),
    }:
        env.pop("CURSOR_CONFIG_DIR")
    return env


def cursor_available(
    binary: str = CURSOR_AGENT,
    *,
    base_env: dict | None = None,
    env_file: Path = CURSOR_ENV_FILE,
) -> bool:
    """Check the local prerequisites without sending the API key anywhere."""
    executable = Path(binary).is_file() if Path(binary).is_absolute() else bool(shutil.which(binary))
    return executable and bool(cursor_env(base_env, env_file).get("CURSOR_API_KEY"))


def cursor_args(
    prompt: str,
    *,
    workspace: str,
    session_id: str | None = None,
    model: str | None = None,
    force: bool = False,
    approve_mcps: bool = False,
    binary: str = CURSOR_AGENT,
) -> list[str]:
    """Build a headless Cursor invocation with machine-readable streaming output."""
    args = [
        binary,
        "--print",
        "--trust",
        "--output-format",
        "stream-json",
        "--stream-partial-output",
        "--workspace",
        workspace,
    ]
    selected_model = model or cursor_model()
    if selected_model:
        args.extend(["--model", selected_model])
    if force:
        args.append("--force")
    if approve_mcps:
        args.append("--approve-mcps")
    if session_id:
        args.extend(["--resume", session_id])
    args.append(prompt)
    return args


def cursor_result_text(stdout: str) -> str:
    """Extract the terminal result text from a Cursor stream-json transcript."""
    result = ""
    deltas = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result" and isinstance(event.get("result"), str):
            result = event["result"]
        elif (
            event.get("type") == "assistant"
            and "timestamp_ms" in event
            and "model_call_id" not in event
        ):
            message = event.get("message")
            raw_content = message.get("content", []) if isinstance(message, dict) else []
            content = raw_content if isinstance(raw_content, list) else []
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                ):
                    deltas.append(part["text"])
    streamed = "".join(deltas)
    # Cursor can emit a stale progress-only `result` while the assistant stream
    # contains the actual final task envelope. Prefer whichever source proves it
    # reached the required completion contract.
    if "<task-output>" in streamed:
        return streamed
    if "<task-output>" in result:
        return result
    return result or streamed


class CursorAgent:
    """Small supervisor facade over the stateless Cursor Agent CLI."""

    def __init__(
        self,
        workspace: str = VAULT,
        *,
        binary: str = CURSOR_AGENT,
        env_file: Path = CURSOR_ENV_FILE,
    ) -> None:
        self.workspace = workspace
        self.binary = binary
        self.env_file = Path(env_file)

    @property
    def alive(self) -> bool:
        return cursor_available(self.binary, env_file=self.env_file)

    async def start(self) -> None:
        if not self.alive:
            raise RuntimeError("Cursor Agent CLI or CURSOR_API_KEY is unavailable")

    async def stop(self) -> None:
        return None

    async def create_session(self) -> str:
        """Create an empty Cursor chat so the first turn can use --resume."""
        # SECURITY-REVIEW: fixed local executable and arguments; the API key is
        # passed only in the child environment and is never logged.
        proc = await asyncio.create_subprocess_exec(
            self.binary,
            "create-chat",
            cwd=self.workspace,
            # `create-chat` runs no tools, so the restrictive tool profile is
            # unnecessary. Cursor currently prints the chat ID but does not exit
            # when CURSOR_CONFIG_DIR points at that profile.
            env=cursor_env(env_file=self.env_file, safe_config=False),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            raw, error = await asyncio.wait_for(proc.communicate(), timeout=20)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("Cursor Agent did not create a chat within 20 seconds")
        session_id = raw.decode(errors="replace").strip()
        if proc.returncode != 0 or not re.fullmatch(r"[0-9a-fA-F-]{20,}", session_id):
            message = error.decode(errors="replace").strip()[:300]
            raise RuntimeError(message or "Cursor Agent could not create a chat")
        return session_id
