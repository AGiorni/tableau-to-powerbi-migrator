"""The migrated project as a git repository: a baseline commit, then one commit per accepted fix.

Every change the refinement agent makes is reviewable (``git log``, ``git diff``) and reversible,
and a rejected change is undone by resetting to the last commit, so the project on disk is always
at the last good state. Only the project's own files are tracked (``<name>.pbip``, the
``.Report`` and ``.SemanticModel`` folders, ``.gitignore``) — never anything else that happens to
be in the output folder. Commits use a neutral ``t2pbi`` identity and fixed line endings, and
never touch the user's git configuration.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_IDENTITY = ["-c", "user.name=t2pbi", "-c", "user.email=t2pbi@localhost", "-c", "core.autocrlf=false"]


class WorkspaceError(RuntimeError):
    """A git command failed."""


class Workspace:
    def __init__(self, pbip_path: str | Path) -> None:
        pbip = Path(pbip_path)
        self.root = pbip.parent
        name = pbip.stem
        self.tracked = [f"{name}.pbip", f"{name}.Report", f"{name}.SemanticModel", ".gitignore"]

    @property
    def exists(self) -> bool:
        return (self.root / ".git").is_dir()

    def init(self, message: str = "Baseline: deterministic migration") -> str:
        """Create the repository (if needed) and commit the project as it is; returns the commit."""

        if not self.exists:
            self._git("init", "--quiet")
        return self.commit(message) or self.head()

    def commit(self, message: str) -> str | None:
        """Commit the project's current files; returns the new commit, or None if nothing changed."""

        self._require()
        self._git("add", "--all", "--", *[p for p in self.tracked if (self.root / p).exists()])
        if not self._git("status", "--porcelain", "--", *self.tracked).strip():
            return None
        self._git("commit", "--quiet", "-m", message)
        return self.head()

    def revert(self) -> None:
        """Throw away every uncommitted change to the project (back to the last commit)."""

        self._require()
        self._git("reset", "--quiet", "--hard", "HEAD")
        self._git("clean", "--quiet", "-fd", "--", *self.tracked)

    def head(self) -> str:
        return self._git("rev-parse", "--short", "HEAD").strip()

    def diff(self) -> str:
        """Uncommitted changes, as a unified diff."""

        self._require()
        return self._git("diff", "HEAD", "--", *self.tracked)

    def log(self) -> list[tuple[str, str]]:
        """(commit, subject) pairs, newest first."""

        self._require()
        out = self._git("log", "--format=%h%x09%s")
        return [tuple(line.split("\t", 1)) for line in out.splitlines() if "\t" in line]

    def _require(self) -> None:
        # Never run git here without our own repository: it would act on any enclosing one.
        if not self.exists:
            raise WorkspaceError(f"{self.root} is not a t2pbi workspace (call init first)")

    def _git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.root), *_IDENTITY, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False,
        )
        if proc.returncode != 0:
            raise WorkspaceError(f"git {args[0]} failed: {proc.stderr.strip()[:300]}")
        return proc.stdout
