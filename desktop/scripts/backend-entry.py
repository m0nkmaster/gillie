"""Frozen CLI entry point; the Claude hook must also work without system Python."""
import runpy
import sys

if sys.argv[1:2] == ["--claude-guard"]:
    del sys.argv[1]
    runpy.run_module("roosta.claude_guard", run_name="__main__")
else:
    from roosta.cli import main
    raise SystemExit(main())
