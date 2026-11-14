"""Development provenance for the technical panel: the running commit and the Entire
checkpoint refs stored in the repository. Read-only; reports what git returns."""

from __future__ import annotations

import subprocess

from fastapi import APIRouter

from app.config import ENGINE_VERSION, REPO_ROOT

router = APIRouter(tags=["meta"])


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=5, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip()


@router.get("/meta/provenance")
def provenance() -> dict:
    refs = _git("for-each-ref", "--format=%(refname)", "refs/entire/checkpoints") or ""
    checkpoints = sorted(r.rsplit("/", 1)[-1] for r in refs.splitlines() if r)
    return {
        "engine_version": ENGINE_VERSION,
        "commit": _git("rev-parse", "HEAD"),
        "commit_subject": _git("log", "-1", "--format=%s"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "entire_checkpoints": checkpoints,
        "entire_checkpoint_count": len(checkpoints),
        "source": "git" if refs or checkpoints else "git (no Entire checkpoint refs found)",
    }
