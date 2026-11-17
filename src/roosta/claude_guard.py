"""Claude Code PreToolUse hook: approve read-shaped MCP calls, block write-shaped ones.

Usage: claude_guard.py <keyword>... (tool input JSON on stdin). The keywords come
from the Cursor profile's `Mcp(*:*<keyword>*)` denials. Matching ignores case, so
camelCase tools such as `addCommentToJiraIssue` are caught too.
"""
import json
import sys


def decide(tool_name: str, keywords: list[str]) -> dict:
    tool = tool_name.split("__", 2)[-1].lower()
    blocked = next((k for k in keywords if k.lower() in tool), None)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny" if blocked else "allow",
            "permissionDecisionReason": (
                f"Roosta profile blocks write-shaped MCP tools ('{blocked}'); "
                "draft the change and ask for approval instead"
                if blocked else "Roosta profile allows read-shaped MCP tools"
            ),
        }
    }


if __name__ == "__main__":
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    print(json.dumps(decide(str(payload.get("tool_name", "")), sys.argv[1:])))
