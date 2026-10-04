"""Prompt files, loaded once and identified by content.

Every ``ModelCall`` event records the prompt's id and sha. When an eval
regresses, that pair says exactly which prompt text produced each run -- the
working tree may have moved on since.

This module imports nothing of ours.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class Prompt:
    #: Repo-relative path, e.g. ``domains/sql/prompts/answer.txt``.
    id: str
    text: str
    #: First 12 hex digits of the SHA-256 of ``text``.
    sha: str


def load_prompt(path: Path) -> Prompt:
    """Read a prompt file. Explicit UTF-8: the platform default differs on Windows."""
    path = path.resolve()
    text = path.read_text(encoding="utf-8")
    try:
        prompt_id = path.relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        prompt_id = path.name
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return Prompt(id=prompt_id, text=text, sha=sha)
