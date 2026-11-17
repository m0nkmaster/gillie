"""Build a relocatable backend using the build machine's native architecture."""
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[2]
output = root / "desktop" / "dist" / "backend"
subprocess.run([
    sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
    "--name", "roosta-backend", "--distpath", str(output),
    "--workpath", str(output / "work"), "--specpath", str(output),
    "--collect-all", "roosta", "--collect-all", "uvicorn", "--collect-submodules", "keyring.backends", "--copy-metadata", "keyring",
    "--hidden-import", "keyring.backends.macOS",
    "--hidden-import", "roosta.app", "--hidden-import", "roosta.claude_guard",
    "--copy-metadata", "roosta",
    str(root / "desktop" / "scripts" / "backend-entry.py"),
], check=True, cwd=root)
