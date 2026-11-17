"""Read-only workspace catalogue and explicit chat attachments."""

from datetime import datetime, timezone
import os
from pathlib import Path
import re
from textwrap import shorten

import yaml

from roosta.pulse import markdown_title


EXCLUDED = {"private", "code", "node_modules", "cache", "__pycache__"}
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_CONTEXT_FILES = 8
MAX_CONTEXT_BYTES = 192 * 1024


def plain_text(value: str) -> str:
    """Flatten Markdown emphasis and links to escapable plain text."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", str(value or ""))
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return re.sub(r"\s+", " ", text).strip()


def strip_frontmatter(content: str) -> str:
    """Return the document body with any frontmatter block removed."""
    if not content.startswith("---"):
        return content
    end = content.find("\n---", 3)
    return content if end == -1 else content[end + 4:]


def metadata(content: str) -> dict:
    match = re.match(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", content, re.S)
    if not match:
        return {}
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def text_values(raw) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    return [value.strip() for value in raw if isinstance(value, str) and value.strip()] if isinstance(raw, list) else []


def document_summary(content: str, front: dict) -> str:
    description = front.get("description") or front.get("summary")
    if isinstance(description, str):
        return shorten(plain_text(description), width=240, placeholder="…")
    body = strip_frontmatter(content)
    overview = re.search(r"^##?\s+(?:Overview|Purpose|Summary|About)\s*\n(.*?)(?=^##?\s|\Z)", body, re.M | re.S | re.I)
    if overview:
        body = overview.group(1)
    body = re.sub(r"```.*?```", "", body, flags=re.S)
    for paragraph in re.split(r"\n\s*\n", body):
        lines = [line.strip() for line in paragraph.splitlines()
                 if line.strip() and not line.lstrip().startswith(("#", "|", "- ", "* ", ">", "!["))]
        if lines:
            return shorten(plain_text(" ".join(lines)), width=240, placeholder="…")
    return ""


def document_kind(path: str, title: str, front: dict) -> str:
    name = Path(path).stem.lower()
    declared = front.get("type")
    declared = declared.lower() if isinstance(declared, str) else ""
    words = re.sub(r"[^a-z]+", " ", name + " " + title.lower()).split()
    if declared in {"decision", "adr"} or {"decision", "decisions", "adr"}.intersection(words):
        return "Decision record"
    if declared in {"plan", "proposal", "roadmap"} or {"plan", "proposal", "roadmap"}.intersection(words):
        return "Plan"
    if path.startswith("projects/") and name == "readme":
        return "Project overview"
    if path.startswith("drafts/"):
        return "Draft"
    if path.startswith("analysis/"):
        return "Research"
    return "Reference" if name in {"readme", "agents", "tools", "context"} else "Notes"


def knowledge_collections(files: list[dict]) -> list[dict]:
    """Group existing metadata and document conventions; never infer claims with AI."""
    collections = []
    projects = {str(Path(file["path"]).parent): file for file in files
                if re.fullmatch(r"projects/[^/]+/README\.md", file["path"], re.I)}
    tag_sources: dict[str, list[dict]] = {}
    for file in files:
        for tag in sorted({tag.lower() for tag in file["tags"]}):
            tag_sources.setdefault(tag, []).append(file)
        parent = projects.get("/".join(file["path"].split("/")[:2]))
        file["project"] = parent["title"] if parent else ""
        file["topics"] = sorted(set(file["tags"] + (parent["tags"] if parent else [])))
        if parent and file is not parent and file["title"].lower() in {"plan", "decisions", "notes", "overview"}:
            file["title"] = parent["title"] + " — " + file["title"]
    for folder, overview in projects.items():
        collections.append({"id": "project:" + folder.split("/")[1], "group": "projects",
                            "title": overview["title"], "summary": overview["summary"],
                            "status": overview["status"], "overview": overview["path"],
                            "paths": [file["path"] for file in files if file["path"].startswith(folder + "/")]})

    acronyms = {word.lower(): word for word in ["AI", "API", "AWS", "CI", "MCP", "OKR", "SDK", "SDKs", "UI", "UX"]}
    for tag, sources in tag_sources.items():
        if len(sources) < 2:
            continue
        paths = [file["path"] for file in files if tag in {value.lower() for value in file["topics"]}]
        names = sorted({file["project"] or file["title"] for file in sources})
        title = " ".join(acronyms.get(word, word.capitalize()) for word in tag.replace("_", "-").split("-"))
        collections.append({"id": "topic:" + tag, "group": "topics", "title": title,
                            "summary": "Across " + ", ".join(names[:3]) + (" and more." if len(names) > 3 else "."),
                            "paths": paths})

    for key, title, kind, heading, summary in [
        ("decisions", "Decisions", "Decision record", r"(?:Key\s+)?Decisions?", "Decision records and documents with a decisions section."),
        ("plans", "Plans & roadmaps", "Plan", r"(?:Current\s+)?(?:Plan|Roadmap|Proposal)s?", "Plans, proposals and roadmaps for what comes next."),
    ]:
        paths = [file["path"] for file in files if file["kind"] == kind
                 or re.search(r"^##\s+" + heading + r"\s*$", file["text"], re.M | re.I)]
        if paths:
            collections.append({"id": "work:" + key, "group": "work", "title": title, "summary": summary, "paths": paths})
    order = {"active": 0, "blocked": 1, "paused": 2, "completed": 3}
    return sorted(collections, key=lambda entry: (order.get(entry.get("status"), 4), entry["title"].casefold()))


def visible_path(path: Path) -> bool:
    return (path.suffix.lower() == ".md"
            and not path.is_absolute()
            and all(part.lower() not in EXCLUDED and not part.startswith(".") for part in path.parts))


def read_document(root: Path, raw: str) -> tuple[Path, str, int]:
    """Constrain both the requested and resolved path, including symlink targets."""
    if not isinstance(raw, str) or not raw or len(raw) > 500 or "\\" in raw:
        raise ValueError("Choose a workspace Markdown document.")
    relative = Path(raw)
    if not visible_path(relative):
        raise ValueError("Choose a workspace Markdown document.")
    vault = root.resolve()
    target = (vault / relative).resolve()
    try:
        canonical = target.relative_to(vault)
    except ValueError as exc:
        raise ValueError("Document is outside the workspace.") from exc
    if not visible_path(canonical):
        raise ValueError("Choose a workspace Markdown document.")
    if not target.is_file():
        raise FileNotFoundError("Document no longer exists.")
    with target.open("rb") as source:
        content = source.read(MAX_DOCUMENT_BYTES + 1)
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ValueError("Document exceeds the 2 MB preview limit.")
    return canonical, content.decode("utf-8"), len(content)


def build_catalogue(root: Path) -> dict:
    files = []
    skipped = 0
    for parent, directories, names in os.walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories
                                if name.lower() not in EXCLUDED and not name.startswith(".")
                                and not (Path(parent) / name).is_symlink())
        for name in sorted(names):
            candidate = Path(parent) / name
            relative = candidate.relative_to(root)
            if not visible_path(relative) or candidate.is_symlink():
                continue
            try:
                canonical, content, size = read_document(root, relative.as_posix())
                modified = datetime.fromtimestamp(candidate.stat().st_mtime, timezone.utc).isoformat()
            except (OSError, UnicodeError, ValueError):
                skipped += 1
                continue
            front = metadata(content)
            title = plain_text(front["title"]) if isinstance(front.get("title"), str) and front["title"].strip() else markdown_title(strip_frontmatter(content), candidate.stem)
            files.append({"path": canonical.as_posix(), "title": title,
                          "text": content, "size": size, "modified": modified,
                          "summary": document_summary(content, front),
                          "kind": document_kind(canonical.as_posix(), title, front),
                          "tags": text_values(front.get("tags")),
                          "status": front.get("status") if isinstance(front.get("status"), str) else ""})
    return {"files": files, "collections": knowledge_collections(files), "skipped": skipped,
            "indexed_at": datetime.now(timezone.utc).isoformat()}


def context_documents(root: Path, paths: list) -> list[dict]:
    if not isinstance(paths, list) or len(paths) > MAX_CONTEXT_FILES:
        raise ValueError(f"Choose up to {MAX_CONTEXT_FILES} context files.")
    documents = []
    seen = set()
    size = 0
    for raw in paths:
        relative, content, count = read_document(root, raw)
        path = relative.as_posix()
        if path in seen:
            continue
        seen.add(path)
        size += count
        if size > MAX_CONTEXT_BYTES:
            raise ValueError("Selected files exceed the 192 KB context limit. Choose fewer files.")
        documents.append({"path": path, "content": content})
    return documents
