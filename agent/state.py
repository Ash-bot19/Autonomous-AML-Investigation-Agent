from __future__ import annotations

from typing import Any, Optional, TypedDict

from models.schemas import EvidenceEntry, EvaluationOutput, InvestigationPayload


class InvestigationStatus:
    IDLE = "IDLE"
    INVESTIGATING = "INVESTIGATING"
    TOOL_CALLING = "TOOL_CALLING"
    EVALUATING = "EVALUATING"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"


class AgentState(TypedDict):
    # Input
    payload: Optional[InvestigationPayload]

    # Runtime tracking
    status: str                        # InvestigationStatus constant
    hop_count: int                     # increments each TOOL_CALLING cycle
    accumulated_cost_usd: float        # sum of LLM call costs
    started_at: Optional[float]        # time.time() at INVESTIGATING entry
    tool_selection: Optional[dict]     # last ToolSelectionOutput dict
    last_tool_result: Optional[dict]   # last ToolResult dict

    # Evidence
    evidence_chain: list[dict]         # list of EvidenceEntry dicts

    # Outputs
    evaluation: Optional[dict]         # EvaluationOutput dict (when set)
    escalation_reason: Optional[str]   # set when routing to ESCALATED
    final_report: Optional[dict]       # ComplianceReport dict (when RESOLVED)
