"""The library and review decisions, carried over from one launch to the next."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..core.model import Decision

log = logging.getLogger(__name__)

SESSION_FILE = "session.json"
_VERSION = 1

PageKey = tuple[str, str]


@dataclass(slots=True)
class SavedDecision:
    decision: Decision
    kept: set[PageKey] = field(default_factory=set)


@dataclass(slots=True)
class Session:
    archives: list[Path] = field(default_factory=list)
    # Keyed by group id, which is stable across rescans.
    decisions: dict[str, SavedDecision] = field(default_factory=dict)


def load_session(path: Path) -> Session | None:
    """The saved session, or None if there is none or it cannot be understood.

    A damaged file is not worth failing startup over; it is simply ignored and
    replaced the next time the session is saved.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable session file %s: %s", path, exc)
        return None
    if not isinstance(raw, dict) or raw.get("version") != _VERSION:
        return None

    session = Session()
    for item in raw.get("archives", []):
        if isinstance(item, str):
            session.archives.append(Path(item))
    decisions = raw.get("decisions", {})
    if isinstance(decisions, dict):
        for gid, saved in decisions.items():
            try:
                decision = Decision(saved["decision"])
                kept = {(str(a), str(n)) for a, n in saved.get("kept", [])}
            except (KeyError, TypeError, ValueError):
                continue
            session.decisions[str(gid)] = SavedDecision(decision, kept)
    return session


def save_session(path: Path, session: Session) -> None:
    """Write the session atomically, so a crash mid-save keeps the previous one."""
    payload = {
        "version": _VERSION,
        "archives": [str(p) for p in session.archives],
        "decisions": {
            gid: {"decision": saved.decision.value, "kept": sorted(saved.kept)}
            for gid, saved in session.decisions.items()
        },
    }
    tmp: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".session-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not save the session to %s: %s", path, exc)
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
