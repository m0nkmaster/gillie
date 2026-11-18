"""Runner-owned GitHub issue updates under a workspace connection's standing grant."""
import json
import os
import re
import subprocess

from roosta import ai_connections
from roosta.cursor_agent import agent_path


def approved_repo(connection, task, skill):
    path = ai_connections.directory(connection) / "issue-grants.json"
    if not path.is_file() or path.is_symlink():
        return ""
    grants = json.loads(path.read_text())
    grant = grants.get(task) if isinstance(grants, dict) else None
    if not isinstance(grant, dict) or grant.get("skill") != skill:
        return ""
    repo = grant.get("repository")
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repo):
        return ""
    return "" if repo.endswith(("/.", "/..")) else repo


def _api(endpoint, *args, payload=None):
    command = ["gh", "api", "--hostname", "github.com", endpoint, *args]
    if payload is not None:
        command.extend(["--method", "POST", "--input", "-"])
    result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                            text=True, capture_output=True, timeout=45, cwd="/",
                            env={**os.environ, "PATH": agent_path(os.environ.get("PATH"))})
    if result.returncode:
        raise ValueError(f"GitHub request failed: {result.stderr.strip()[:300]}")
    return result.stdout


def apply_update(repo, update):
    if not isinstance(update, dict) or set(update) != {"issue", "comment", "complete"}:
        raise ValueError("Expected an issue number, comment and completion flag")
    issue, comment, complete = update["issue"], update["comment"], update["complete"]
    if type(issue) is not int or not 0 < issue <= 2**31 - 1 or type(complete) is not bool:
        raise ValueError("Invalid issue number or completion flag")
    if not isinstance(comment, str) or not comment.strip() or len(comment) > 8000:
        raise ValueError("Expected a non-empty issue comment of up to 8000 characters")
    endpoint = f"repos/{repo}/issues/{issue}"
    info = json.loads(_api(endpoint))
    url = f"https://github.com/{repo}/issues/{issue}"
    labels = {label["name"] for label in info["labels"]}
    if info["html_url"].lower() != url.lower() or "pull_request" in info or info["state"] != "open" or "ready" not in labels:
        raise ValueError(f"Standing approval requires an open ready issue at {url}")
    # A retry after a label failure or an ambiguous POST must not repeat the comment.
    existing = _api(endpoint + "/comments", "--paginate", "--jq", ".[] | .body | @json")
    if not any(json.loads(line) == comment for line in existing.splitlines()):
        _api(endpoint + "/comments", payload={"body": comment})
    if complete and "in review" not in labels:
        added = json.loads(_api(endpoint + "/labels", payload={"labels": ["in review"]}))
        if not any(label["name"] == "in review" for label in added):
            raise ValueError(f"Comment present on {url}, but GitHub did not apply the in review label")
    return f"Comment present on {url}" + (" with the in review label." if complete else ".")
