"""Chat upload storage: sanitised names, size limits and validated lookups."""

from __future__ import annotations
import hashlib, re, shutil, uuid
from pathlib import Path

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_CHAT_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_MESSAGE_UPLOADS = 8
MAX_ITEM_ID = 520
UPLOAD_ID = re.compile(r"[0-9a-f]{12}")


def upload_dir(root: Path, item_id: str) -> Path:
    return Path(root) / hashlib.sha256(item_id.encode()).hexdigest()[:24]


def safe_name(raw) -> str:
    if not isinstance(raw, str) or not raw:
        return "file"
    name = re.split(r"[/\\]", raw)[-1]
    name = "".join(ch for ch in name if ch.isprintable())
    name = re.sub(r"[^A-Za-z0-9._ ()-]", "_", name).lstrip(".").strip()
    if len(name) > 100:
        suffix = Path(name).suffix[:20]
        name = name[:100 - len(suffix)] + suffix if suffix != name else name[:100]
    return name.strip() or "file"


def image_type(head: bytes) -> str:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return ""


def save(root: Path, item_id: str, name, data: bytes) -> dict:
    if not isinstance(item_id, str) or not item_id or len(item_id) > MAX_ITEM_ID:
        raise ValueError("Need a valid chat id.")
    if not data:
        raise ValueError("File is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("File exceeds the 20 MB upload limit.")
    directory = upload_dir(root, item_id)
    used = sum(p.stat().st_size for p in directory.iterdir() if p.is_file()) if directory.is_dir() else 0
    if used + len(data) > MAX_CHAT_UPLOAD_BYTES:
        raise ValueError("This chat has reached its 200 MB upload limit.")
    upload_id = uuid.uuid4().hex[:12]
    clean = safe_name(name)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{upload_id}-{clean}").write_bytes(data)
    return {"id": upload_id, "name": clean, "size": len(data), "image": bool(image_type(data[:16]))}


def resolve(root: Path, item_id: str, upload_id) -> Path:
    if not isinstance(upload_id, str) or not UPLOAD_ID.fullmatch(upload_id):
        raise ValueError("Invalid attachment id.")
    directory = upload_dir(root, item_id)
    if directory.is_dir():
        for path in directory.glob(f"{upload_id}-*"):
            if path.is_symlink() or not path.is_file():
                raise ValueError("Invalid attachment.")
            resolved = path.resolve()
            if not resolved.is_relative_to(directory.resolve()):
                raise ValueError("Invalid attachment.")
            return resolved
    raise FileNotFoundError(upload_id)


def describe(root: Path, item_id: str, upload_ids) -> list[dict]:
    if not isinstance(upload_ids, list) or len(upload_ids) > MAX_MESSAGE_UPLOADS:
        raise ValueError(f"Attach up to {MAX_MESSAGE_UPLOADS} files per message.")
    files = []
    seen = []
    for upload_id in upload_ids:
        if upload_id in seen:
            continue
        seen.append(upload_id)
        path = resolve(root, item_id, upload_id)
        with path.open("rb") as handle:
            head = handle.read(16)
        files.append({"id": upload_id, "name": path.name[len(upload_id) + 1:], "size": path.stat().st_size,
                      "image": bool(image_type(head)), "path": str(path)})
    return files


def remove(root: Path, item_id: str) -> None:
    shutil.rmtree(upload_dir(root, item_id), ignore_errors=True)
