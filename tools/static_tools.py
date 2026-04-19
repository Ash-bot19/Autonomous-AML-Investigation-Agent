"""
tools/static_tools.py — Static data-backed AML investigation tools.

Tool:
  - watchlist_lookup: checks entity name against OFAC-style CSV loaded at module import.

Normalisation (D-10): lowercase + strip whitespace only. Exact match against normalised key.
CSV loaded once into a memory dict at module import — no per-lookup file I/O.

Never raises — returns ToolResult(success=False) on any exception.
"""
from __future__ import annotations

import csv
import os
import structlog
from typing import Any

from models.schemas import ToolResult, WatchlistInput

log = structlog.get_logger()

# ── Watchlist memory dict ────────────────────────────────────────────────────
# Keyed by normalised entity name (lowercase + strip).
# Value is the raw CSV row dict.

_WATCHLIST: dict[str, dict[str, Any]] = {}


def _load_watchlist() -> dict[str, dict[str, Any]]:
    """
    Load data/watchlist.csv into a memory dict at module import.
    Key: entity_name.lower().strip() — D-10 normalisation.
    Logs error and returns empty dict if file missing or unreadable.
    """
    csv_path = os.path.join(os.path.dirname(__file__), "..", "data", "watchlist.csv")
    result: dict[str, dict[str, Any]] = {}
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = row["entity_name"].lower().strip()
                result[key] = dict(row)
        log.info("watchlist.loaded", count=len(result))
    except Exception as exc:
        log.error("watchlist.load_failed", error=str(exc))
    return result


_WATCHLIST = _load_watchlist()


# ── watchlist_lookup ─────────────────────────────────────────────────────────


def watchlist_lookup(inp: WatchlistInput) -> ToolResult:
    """
    Check entity_name against the OFAC-style watchlist (CSV-backed memory dict).

    Normalisation: lowercase + strip whitespace (D-10). Exact match only.
    Returns match=True with matched_entity if found, match=False otherwise.
    Never raises — any exception returns ToolResult(success=False).

    Data shape (match):
      {"queried_name": str, "match": True, "matched_entity": str}
    Data shape (no match):
      {"queried_name": str, "match": False, "matched_entity": None}
    """
    try:
        normalised = inp.entity_name.lower().strip()
        row = _WATCHLIST.get(normalised)

        if row:
            log.info(
                "watchlist_lookup.match",
                queried=inp.entity_name,
                matched=row["entity_name"],
            )
            return ToolResult(
                success=True,
                tool_name="watchlist_lookup",
                data={
                    "queried_name": inp.entity_name,
                    "match": True,
                    "matched_entity": row["entity_name"],
                },
            )

        log.info("watchlist_lookup.no_match", queried=inp.entity_name)
        return ToolResult(
            success=True,
            tool_name="watchlist_lookup",
            data={
                "queried_name": inp.entity_name,
                "match": False,
                "matched_entity": None,
            },
        )

    except Exception as exc:
        log.error("watchlist_lookup.error", entity_name=inp.entity_name, error=str(exc))
        return ToolResult(
            success=False,
            tool_name="watchlist_lookup",
            error=str(exc),
        )
