"""
tools/mock_dispatcher.py — Hardcoded mock tool responses for Phase 2 state machine testing.

Phase 3 will replace MOCK_RESPONSES and dispatch_tool internals with real
PostgreSQL/Redis/Kafka implementations. The function signatures stay identical.
"""
from __future__ import annotations

import structlog
from typing import Any

from models.schemas import ToolResult, ToolSelectionOutput

log = structlog.get_logger()

# ── Hardcoded mock responses ─────────────────────────────────────────────────
# Each key is a tool name. Value is the `data` dict returned in ToolResult.
# These are realistic but not real — they exercise the state machine paths.

MOCK_RESPONSES: dict[str, dict[str, Any]] = {
    "txn_history_query": {
        "account_id": "ACC_001",
        "transactions": [
            {"txn_id": "TXN_A", "amount": 150000, "counterparty": "ACC_002", "timestamp": "2026-04-16T10:00:00Z"},
            {"txn_id": "TXN_B", "amount": 200000, "counterparty": "ACC_002", "timestamp": "2026-04-16T10:15:00Z"},
            {"txn_id": "TXN_C", "amount": 175000, "counterparty": "ACC_002", "timestamp": "2026-04-16T10:30:00Z"},
        ],
        "total_90d_count": 12,
        "total_90d_volume_inr": 2100000,
    },
    "counterparty_risk_lookup": {
        "account_id": "ACC_002",
        "risk_tier": "high",
        "flag_reason": "Associated with 3 previously flagged investigations",
    },
    "velocity_check": {
        "account_id": "ACC_001",
        "windows": {
            "1h":  {"count": 3,  "volume_inr": 525000},
            "6h":  {"count": 18, "volume_inr": 3150000},
            "24h": {"count": 23, "volume_inr": 4025000},
        },
    },
    "watchlist_lookup": {
        "queried_name": "ACC_002",
        "match": False,
        "matched_entity": None,
    },
    "round_trip_detector": {
        "cycle_detected": False,
        "cycle_path": None,
        "window_hours": 24,
    },
    "kafka_lag_check": {
        "consumer_group": "aml-investigation-agent",
        "lag": 0,
        "is_pipeline_delay": False,
    },
}

# Order in which mock investigations call tools (used by mock_tool_selection)
MOCK_TOOL_SEQUENCE = [
    "velocity_check",
    "txn_history_query",
    "counterparty_risk_lookup",
]


def dispatch_tool(tool_name: str, tool_input: dict[str, Any]) -> ToolResult:
    """
    Execute a tool by name and return a structured ToolResult.

    Never raises. Returns ToolResult(success=False, error=...) on any failure.
    Phase 3 replaces the body of this function with real implementations.
    The signature is locked.
    """
    try:
        if tool_name not in MOCK_RESPONSES:
            log.warning("dispatch_tool.unknown", tool_name=tool_name)
            return ToolResult(
                success=False,
                tool_name=tool_name,
                error=f"Unknown tool: {tool_name}",
            )

        data = MOCK_RESPONSES[tool_name]
        log.info("dispatch_tool.ok", tool_name=tool_name, input_keys=list(tool_input.keys()))
        return ToolResult(success=True, tool_name=tool_name, data=data)

    except Exception as exc:
        log.error("dispatch_tool.error", tool_name=tool_name, error=str(exc))
        return ToolResult(success=False, tool_name=tool_name, error=str(exc))


def mock_tool_selection(hop_count: int) -> ToolSelectionOutput:
    """
    Deterministic mock for LLM tool selection — returns a tool from MOCK_TOOL_SEQUENCE
    based on hop_count. Wraps around if hop_count exceeds sequence length.

    Phase 4 replaces this with a real LLM call returning ToolSelectionOutput.
    The return type is locked.
    """
    tool_name = MOCK_TOOL_SEQUENCE[hop_count % len(MOCK_TOOL_SEQUENCE)]
    return ToolSelectionOutput(
        tool_name=tool_name,
        tool_input={"account_id": "ACC_001"},
        reasoning=f"Mock selection for hop {hop_count}: chose {tool_name}",
    )
