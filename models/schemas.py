from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

import structlog
from pydantic import BaseModel, Field, model_validator

log = structlog.get_logger()

# ── Trigger / Input ──────────────────────────────────────────────────────────


class InvestigationPayload(BaseModel):
    investigation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    txn_id: str
    trigger_type: Literal["rule_engine", "ml_score", "both"]
    trigger_detail: str
    risk_score: Optional[float] = None
    triggered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ── Tool I/O ─────────────────────────────────────────────────────────────────


class ToolResult(BaseModel):
    success: bool
    tool_name: str
    data: Optional[dict[str, Any]] = None
    error: Optional[str] = None  # populated when success=False

    @model_validator(mode="after")
    def check_data_on_success(self) -> "ToolResult":
        if self.success and self.data is None:
            raise ValueError("ToolResult.data must not be None when success=True")
        return self


# ── LLM Structured Outputs (mocked in Phase 2) ──────────────────────────────

VALID_TOOLS = Literal[
    "txn_history_query",
    "counterparty_risk_lookup",
    "velocity_check",
    "watchlist_lookup",
    "round_trip_detector",
    "kafka_lag_check",
]


class ToolSelectionOutput(BaseModel):
    tool_name: VALID_TOOLS
    tool_input_json: str  # JSON-encoded tool parameters — dict[str, Any] rejected by OpenAI strict schema
    reasoning: str


class EvaluationOutput(BaseModel):
    confidence: float = Field(ge=0.0, le=1.0)
    verdict: Literal["suspicious", "clean", "inconclusive"]
    finding: str
    recommendation: Literal[
        "escalate_to_compliance", "file_SAR", "close_clean", "monitor"
    ]
    narrative: str
    should_continue: bool = Field(
        description="True = call another tool; False = ready to finalize verdict"
    )


# ── Evidence Chain entry ─────────────────────────────────────────────────────


class EvidenceEntry(BaseModel):
    hop: int
    tool: str
    finding: str
    significance: Literal["high", "medium", "low"]


# ── Compliance Report (RESOLVED output) ─────────────────────────────────────


class ComplianceReport(BaseModel):
    investigation_id: str
    txn_id: str
    verdict: Literal["suspicious", "clean", "inconclusive"]
    confidence: float
    finding: str
    evidence_chain: list[EvidenceEntry]
    recommendation: Literal[
        "escalate_to_compliance", "file_SAR", "close_clean", "monitor"
    ]
    narrative: str
    total_hops: int
    total_cost_usd: float
    resolved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ── Escalation Record ────────────────────────────────────────────────────────


class EscalationRecord(BaseModel):
    investigation_id: str
    txn_id: str
    escalation_reason: Literal[
        "low_confidence", "max_hops", "timeout", "cost_cap", "empty_evidence"
    ]
    confidence: Optional[float] = None
    partial_report: Optional[dict[str, Any]] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ── API Response Models (Phase 6) ───────────────────────────────────────────


class StatusResponse(BaseModel):
    investigation_id: str
    status: str          # latest state_to from investigation_audit_log
    hops: int            # count of 'tool_call' event_type rows
    cost_usd: float      # SUM of cost_usd_delta from all rows for this investigation_id


class ReportNotFoundResponse(BaseModel):
    investigation_id: str
    detail: str = "investigation not found or not yet resolved"


# ── Tool Input Models (Phase 3) ─────────────────────────────────────────────


class TxnHistoryInput(BaseModel):
    account_id: str


class CounterpartyRiskInput(BaseModel):
    account_id: str


class VelocityCheckInput(BaseModel):
    account_id: str


class WatchlistInput(BaseModel):
    entity_name: str


class RoundTripInput(BaseModel):
    account_id: str
    window_hours: int = Field(default=24, ge=1, le=168)


class KafkaLagInput(BaseModel):
    # consumer group read at runtime from KAFKA_CONSUMER_GROUP env var
    pass
