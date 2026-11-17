"""Exercise the shipped backend in an isolated home with no developer PATH."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

binary = str(Path(sys.argv[1]).resolve())
with tempfile.TemporaryDirectory(prefix="roosta-smoke-") as directory:
    home = Path(directory)
    workspace = home / "Workspace with spaces"
    env = {"HOME": directory, "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}
    subprocess.run([binary, "--version"], env=env, check=True)
    subprocess.run([binary, "init", "--skip-checks", "--no-git", str(workspace)], env=env, check=True)
    assert (workspace / ".roosta/tasks.yaml").is_file()
    guard = subprocess.run([binary, "--claude-guard", "send"], input='{"tool_name":"mcp__mail__send"}',
                           text=True, capture_output=True, env=env, check=True)
    assert json.loads(guard.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (home / "runner.log").open("w+") as log:
        process = subprocess.Popen([binary, "start", "--workspace", str(workspace), "--port", str(port)],
                                   env=env, stdout=log, stderr=log)
        try:
            for attempt in range(120):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/tasks", timeout=1) as response:
                        assert response.status == 200
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/assistant", timeout=2) as response:
                        assert b"<html" in response.read().lower()
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ai/connections", timeout=5) as response:
                        setup = json.load(response)
                        assert {"openai", "anthropic", "openrouter"} <= setup["providers"].keys()
                        assert setup["connections"] == []
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/settings", timeout=2) as response:
                        assert b'id="ai-connections"' in response.read()
                    break
                except (OSError, TimeoutError):
                    if process.poll() is not None:
                        raise RuntimeError("Backend exited")
                    time.sleep(.25)
            else:
                raise RuntimeError("Backend did not become healthy")
        except Exception:
            log.seek(0)
            print(log.read(), file=sys.stderr)
            raise
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
print("Packaged backend smoke test passed")
