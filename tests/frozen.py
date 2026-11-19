"""Test helper: a temporary git repository with protocol/PROTOCOL.md committed and its hash in FROZEN.txt."""
import subprocess
from pathlib import Path


def git(root: Path, *args: str) -> str:
    cmd = ["git", "-C", str(root), "-c", "user.name=noroshi-test", "-c", "user.email=test@example.invalid",
           "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args]
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def freeze(root: Path, text: str = "# Protocol\n\nFrozen for the test.\n") -> Path:
    """Commit protocol/PROTOCOL.md under `root` and write that commit's hash to protocol/FROZEN.txt."""
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        git(root, "init", "-q")
    (root / "protocol").mkdir(exist_ok=True)
    (root / "protocol" / "PROTOCOL.md").write_text(text)
    git(root, "add", "protocol/PROTOCOL.md")
    git(root, "commit", "-q", "-m", "freeze the protocol")
    frozen = root / "protocol" / "FROZEN.txt"
    frozen.write_text(git(root, "rev-parse", "HEAD") + "\n")
    return frozen
