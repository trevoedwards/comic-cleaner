"""The library and review decisions, carried over from one launch to the next."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ..core.model import Decision, DuplicateGroup

log = logging.getLogger(__name__)

SESSION_FILE = "session.json"
_VERSION = 1

PageKey = tuple[str, str]


@dataclass(slots=True)
class SavedDecision:
    decision: Decision
    kept: set[PageKey] = field(default_factory=set)
    # Every page the group held, so the decision can find the group again when
    # its id changes. Empty for decisions saved before this was recorded.
    pages: set[PageKey] = field(default_factory=set)


@dataclass(slots=True)
class Session:
    archives: list[Path] = field(default_factory=list)
    # Keyed by group id, which survives most rescans; see carry_decisions().
    decisions: dict[str, SavedDecision] = field(default_factory=dict)


def carry_decisions(
    prior: Mapping[str, SavedDecision], groups: list[DuplicateGroup]
) -> tuple[dict[str, SavedDecision], set[str]]:
    """Match earlier decisions to freshly built groups.

    Returns the decision for each new group id that has one, and the prior ids
    that were used up (matched, or dropped as ambiguous).

    A group keeps its id unless a similar page with a lower hash joins it. So a
    decision goes first to the group with the same id; failing that, it follows
    its pages to the one new group holding most of them. Two earlier decisions
    landing on the same new group is ambiguous, and it is left undecided rather
    than given either one.
    """
    by_gid = {group.gid: group for group in groups}
    result: dict[str, SavedDecision] = {}
    used: set[str] = set()
    for gid, saved in prior.items():
        if gid in by_gid:
            result[gid] = saved
            used.add(gid)

    owner = {page.key: group.gid for group in groups for page in group.pages}
    claims: dict[str, list[str]] = {}
    for gid, saved in prior.items():
        if gid in used or not saved.pages:
            continue
        tally = Counter(owner[key] for key in saved.pages if key in owner)
        if not tally:
            continue
        target, overlap = tally.most_common(1)[0]
        if overlap * 2 > len(saved.pages):  # most of the old group's pages
            claims.setdefault(target, []).append(gid)

    for target, sources in claims.items():
        used.update(sources)
        if target in result or len(sources) > 1:
            continue
        result[target] = prior[sources[0]]
    return result, used


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
                pages = {(str(a), str(n)) for a, n in saved.get("pages", [])}
            except (KeyError, TypeError, ValueError):
                continue
            session.decisions[str(gid)] = SavedDecision(decision, kept, pages)
    return session


def save_session(path: Path, session: Session) -> None:
    """Write the session atomically, so a crash mid-save keeps the previous one."""
    payload = {
        "version": _VERSION,
        "archives": [str(p) for p in session.archives],
        "decisions": {
            gid: {
                "decision": saved.decision.value,
                "kept": sorted(saved.kept),
                "pages": sorted(saved.pages),
            }
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
