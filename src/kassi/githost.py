"""LocalGitHost — diff reader via subprocess (the one non-k6 shell-out kassi keeps)."""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitHostError(Exception):
    """Base class for GitHost failures."""


class GitHostNotFoundError(GitHostError):
    """Repo or ref does not exist."""


def get_diff(repo_path: Path, ref: str) -> str:
    """Return ``git diff <ref>..HEAD`` as a unified diff string."""
    try:
        subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise GitHostNotFoundError(f"Not inside a git repo: {repo_path}") from exc

    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "diff", f"{ref}..HEAD", "--", str(repo_path)],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise GitHostError(f"git diff failed: {exc.stderr}") from exc
    return result.stdout
