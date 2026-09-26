"""Writing a removal plan down, for review or record, without acting on it."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .. import APP_NAME, __version__
from .remover import RemovalPlan

FILE_FORMAT = "comiccleaner-plan"
FILE_VERSION = 1

CSV_COLUMNS = [
    "archive", "status", "reason", "pages_removed", "original_pages", "percent",
    "dry_run", "removed_entries",
]

# Why a book is in the plan file but will not be touched.
SKIPPED_OVER_LIMIT = "would lose too large a share of its pages"
SKIPPED_PROTECTED = "in a protected folder"


def describe_plan(plan: RemovalPlan, names: int = 3) -> str:
    """One line for a book: how much goes, and the first few pages by name."""
    listed = plan.ordered_names
    shown = ", ".join(listed[:names])
    more = f" and {len(listed) - names} more" if len(listed) > names else ""
    total = f" of {plan.original_pages}" if plan.original_pages else ""
    return (
        f"removing {len(listed)}{total} ({plan.fraction:.0%}), "
        f"{max(plan.remaining_pages, 0)} left: {shown}{more}"
    )


def _record(plan: RemovalPlan, status: str, reason: str, dry_run: bool) -> dict[str, Any]:
    return {
        "archive": str(plan.archive),
        "status": status,
        "reason": reason,
        "pages_removed": len(plan.remove_names),
        "original_pages": plan.original_pages,
        "percent": round(plan.fraction * 100, 1),
        "dry_run": dry_run,
        "removed_entries": plan.ordered_names,
    }


def plan_records(
    plans: Iterable[RemovalPlan],
    skipped: Iterable[tuple[RemovalPlan, str]] = (),
    *,
    dry_run: bool,
) -> list[dict[str, Any]]:
    """Every book the plan covers: those to be cleaned, then those left out."""
    rows = [_record(plan, "planned", "", dry_run) for plan in plans]
    rows += [_record(plan, "skipped", reason, dry_run) for plan, reason in skipped]
    return rows


def write_plan_json(path: Path, records: list[dict[str, Any]], *, dry_run: bool) -> None:
    payload = {
        "format": FILE_FORMAT,
        "version": FILE_VERSION,
        "written_by": f"{APP_NAME} {__version__}",
        "dry_run": dry_run,
        "books": records,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_plan_csv(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for row in records:
            writer.writerow([
                row["archive"], row["status"], row["reason"], row["pages_removed"],
                row["original_pages"], row["percent"], "yes" if row["dry_run"] else "no",
                "; ".join(row["removed_entries"]),
            ])
