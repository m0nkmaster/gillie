"""The user's profile: who the assistant works for and how it should sound.

`PROFILE.md` at the workspace root is plain Markdown with YAML frontmatter, so people edit it
like any other note. Prompts read it instead of hardcoding a name. Every field is optional.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from roosta import paths

FIELDS = ("name", "short_name", "pronouns", "role", "assistant_name", "tone", "language",
          "timezone", "hours")


@dataclass(frozen=True)
class Profile:
    name: str = ""
    short_name: str = ""
    pronouns: str = ""
    role: str = ""
    assistant_name: str = "Assistant"
    tone: str = "concise and direct"
    language: str = ""
    timezone: str = ""
    hours: str = ""

    @property
    def user(self) -> str:
        """What prompts call the user: the short name, else first name, else 'the user'."""
        if self.short_name:
            return self.short_name
        return self.name.split()[0] if self.name.split() else "the user"

    @property
    def reviewer(self) -> str:
        """The OKF actor recorded when the user verifies a document."""
        slug = re.sub(r"[^a-z0-9]+", "-", (self.name or self.user).lower()).strip("-")
        return f"human:{slug or 'user'}"

    def style(self) -> str:
        """One sentence of voice guidance for prompts."""
        parts = [f"Tone: {self.tone}."] if self.tone else []
        if self.language:
            parts.append(f"Write in {self.language}.")
        if self.timezone:
            parts.append(f"{self.user}'s timezone is {self.timezone}.")
        if self.hours:
            parts.append(f"Working hours: {self.hours}.")
        return " ".join(parts)

    def public(self) -> dict:
        return {"name": self.name, "user": self.user, "assistant_name": self.assistant_name,
                "reviewer": self.reviewer}


def parse(text: str) -> Profile:
    match = re.match(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", text or "", re.S)
    if not match:
        return Profile()
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return Profile()
    if not isinstance(data, dict):
        return Profile()
    values = {key: str(data[key]).strip() for key in FIELDS
              if isinstance(data.get(key), (str, int, float)) and str(data[key]).strip()}
    return Profile(**values)


_cache: tuple[float, Profile] | None = None


def load(path: Path | None = None) -> Profile:
    """Read the profile, re-parsing only when the file changes."""
    global _cache
    path = path or paths.PROFILE_FILE
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return Profile()
    if path == paths.PROFILE_FILE and _cache and _cache[0] == mtime:
        return _cache[1]
    try:
        profile = parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return Profile()
    if path == paths.PROFILE_FILE:
        _cache = (mtime, profile)
    return profile
