from __future__ import annotations

import time

import structlog
from langgraph.graph import END, StateGraph

from agent.audit_trail import write_audit_event
from agent.escalation_writer import build_partial_report, write_escalation
from agent.evidence import build_evidence_chain
from agent.llm_client import call_llm
from agent.report_writer import write_compliance_report
from agent.state import AgentState, InvestigationStatus
from models.schemas import EvaluationOutput, ToolSelectionOutput
from tools.dispatcher import dispatch_tool

log = structlog.get_logger()

# ── Tool descriptions for LLM prompting (LLM-01) ─────────────────────────────

TOOL_DESCRIPTIONS: dict[str, str] = {
    "txn_history_query": "Last 90 days of transactions for an account_id (Postgres).",
    "counterparty_risk_lookup": "Risk tier (low/medium/high) and flag reason for an account_id.",
    "velocity_check": "Transaction count + volume in last 1h/6h/24h (Redis rolling window).",
    "watchlist_lookup": "OFAC-style watchlist match by entity_name (CSV).",
    "round_trip_detector": "Detect A→B→C→A cycles for an account_id within window_hours.",
    "kafka_lag_check": "Kafka consumer-group lag — rules out pipeline delay as false positive.",
}

SYSTEM_PROMPT_TOOL_SELECTION: str = (
    "You are an AML investigation agent selecting the next tool to investigate "
    "a flagged transaction. You must return exactly one tool call. "
    "Do not call a tool you have already called in this investigation unless the "
    "evidence requires it. Available tools:\n"
    + "\n".join(f"- {name}: {desc}" for name, desc in TOOL_DESCRIPTIONS.items())
)


def _build_tool_selection_user_message(state: dict) -> str:
    """Build the user message describing the current investigation state."""
    payload = state.get("payload")
    evidence = state.get("evidence_chain", [])
    hop = state.get("hop_count", 0)

    payload_summary = (
        f"Investigation {payload.investigation_id} — txn_id={payload.txn_id}, "
        f"trigger={payload.trigger_type} ({payload.trigger_detail}), "
        f"risk_score={payload.risk_score}"
    ) if payload else "(no payload)"

    evidence_summary = (
        "\n".join(
            f"  hop {e['hop']}: {e['tool']} — {e['finding']} (significance={e['significance']})"
            for e in evidence
        ) or "  (no evidence yet)"
    )

    return (
        f"{payload_summary}\n"
        f"Current hop: {hop}\n"
        f"Evidence so far:\n{evidence_summary}\n"
        f"Select the next tool. Provide tool_name, tool_input dict, and one-sentence reasoning."
    )


SYSTEM_PROMPT_EVALUATION: str = (
    "You are an AML investigation analyst evaluating evidence collected by tool calls. "
    "Decide:\n"
    "  - confidence (0.0-1.0): how confident you are in your verdict\n"
    "  - verdict: suspicious | clean | inconclusive\n"
    "  - finding: one-sentence summary of what the evidence shows\n"
    "  - recommendation: escalate_to_compliance | file_SAR | close_clean | monitor\n"
    "  - narrative: 2-4 sentences explaining the verdict\n"
    "  - should_continue: True if you need another tool call to be confident; "
    "False if the current evidence is sufficient to issue a verdict\n"
    "\n"
    "You may only reference evidence from the tool results provided. "
    "Do not infer additional patterns not present in the data."
)


def _build_evaluation_user_message(state: dict) -> str:
    payload = state.get("payload")
    evidence = state.get("evidence_chain", [])
    hop = state.get("hop_count", 0)
    cost = state.get("accumulated_cost_usd", 0.0)

    payload_summary = (
        f"Investigation {payload.investigation_id} — txn_id={payload.txn_id}, "
        f"trigger={payload.trigger_type} ({payload.trigger_detail}), "
        f"risk_score={payload.risk_score}"
    ) if payload else "(no payload)"

    evidence_block = "\n".join(
        f"  hop {e['hop']}: {e['tool']} — {e['finding']} (significance={e['significance']})"
        for e in evidence
    ) or "  (no evidence)"

    return (
        f"{payload_summary}\n"
        f"Hops used so far: {hop} (max 4)\n"
        f"Accumulated cost: ${cost:.4f} (cap $0.05)\n"
        f"Evidence:\n{evidence_block}\n"
        f"\nProvide your evaluation."
    )


# ── Node implementations ─────────────────────────────────────────────────────


def node_investigating(state: AgentState) -> AgentState:
    """INVESTIGATING: initialises runtime fields and uses LLM to select first tool (LLM-01)."""
    payload = state.get("payload")
    log.info("node.investigating", investigation_id=payload.investigation_id if payload else None)

    # Initial state init
    initial_cost = state.get("accumulated_cost_usd", 0.0) or 0.0
    initial_chain = state.get("evidence_chain", []) or []
    started_at = state.get("started_at") or time.time()

    payload_id = payload.investigation_id if payload else "unknown"

    # AUDT-01: triggered row — investigation payload received
    write_audit_event(
        investigation_id=payload_id,
        event_type="triggered",
        event_detail={
            "trigger_type": payload.trigger_type if payload else None,
            "trigger_detail": payload.trigger_detail if payload else None,
            "risk_score": payload.risk_score if payload else None,
        },
        state_from=None,
        state_to=InvestigationStatus.INVESTIGATING,
    )

    # AUDT-02: state_change row — IDLE → INVESTIGATING
    write_audit_event(
        investigation_id=payload_id,
        event_type="state_change",
        state_from=InvestigationStatus.IDLE,
        state_to=InvestigationStatus.INVESTIGATING,
    )

    user_msg = _build_tool_selection_user_message({
        **state, "hop_count": 0, "evidence_chain": initial_chain
    })

    selection, cost_usd = call_llm(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_TOOL_SELECTION},
            {"role": "user", "content": user_msg},
        ],
        response_model=ToolSelectionOutput,
    )

    if isinstance(selection, ToolSelectionOutput):
        selection_dict = selection.model_dump()
    else:
        # call_llm returned the fallback EvaluationOutput on error.
        # Escalate immediately — calling a tool with bad input produces a false-clean signal.
        log.warning(
            "node.investigating.llm_fallback_escalating",
            investigation_id=payload.investigation_id if payload else None,
        )
        # AUDT-02: state_change row for fallback escalation path (INVESTIGATING → ESCALATED)
        write_audit_event(
            investigation_id=payload_id,
            event_type="state_change",
            state_from=InvestigationStatus.INVESTIGATING,
            state_to=InvestigationStatus.ESCALATED,
        )
        return {
            **state,
            "status": InvestigationStatus.ESCALATED,
            "started_at": started_at,
            "hop_count": 0,
            "accumulated_cost_usd": initial_cost + cost_usd,
            "evidence_chain": initial_chain,
            "tool_selection": None,
            "escalation_reason": "low_confidence",
        }

    return {
        **state,
        "status": InvestigationStatus.INVESTIGATING,
        "started_at": started_at,
        "hop_count": 0,
        "accumulated_cost_usd": initial_cost + cost_usd,
        "evidence_chain": initial_chain,
        "tool_selection": selection_dict,
    }


def node_tool_calling(state: AgentState) -> AgentState:
    """TOOL_CALLING: executes selected tool, appends evidence, picks next tool via LLM (LLM-01)."""
    from models.schemas import EvidenceEntry

    import json as _json

    tool_sel = state.get("tool_selection") or {}
    tool_name = tool_sel.get("tool_name", "velocity_check")
    raw_input = tool_sel.get("tool_input_json") or tool_sel.get("tool_input") or "{}"
    tool_input = _json.loads(raw_input) if isinstance(raw_input, str) else raw_input
    hop = state.get("hop_count", 0)

    log.info("node.tool_calling", tool=tool_name, hop=hop)
    payload = state.get("payload")
    investigation_id = payload.investigation_id if payload else "unknown"
    result = dispatch_tool(tool_name, tool_input, investigation_id, hop)

    # AUDT-03: tool_call row — after each deterministic tool dispatch
    write_audit_event(
        investigation_id=investigation_id,
        event_type="tool_call",
        event_detail={"tool_name": tool_name, "hop": hop + 1},
        cost_usd_delta=0.0,  # tool calls are free; LLM selection cost tracked separately
    )

    significance = "high" if result.success else "low"
    entry = EvidenceEntry(
        hop=hop + 1,
        tool=tool_name,
        finding=str(result.data) if result.success else f"Tool error: {result.error}",
        significance=significance,
    )

    new_hop_count = hop + 1
    new_evidence = state.get("evidence_chain", []) + [entry.model_dump()]

    # LLM picks the NEXT tool for the upcoming EVALUATING→TOOL_CALLING cycle (LLM-01)
    next_user_msg = _build_tool_selection_user_message({
        **state, "hop_count": new_hop_count, "evidence_chain": new_evidence
    })
    next_selection, cost_usd = call_llm(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_TOOL_SELECTION},
            {"role": "user", "content": next_user_msg},
        ],
        response_model=ToolSelectionOutput,
    )

    if isinstance(next_selection, ToolSelectionOutput):
        next_selection_dict = next_selection.model_dump()
        escalation_reason = None
    else:
        # Escalate immediately rather than call a tool with bad input — txn_id is not
        # account_id and a velocity_check with wrong input produces a false-clean signal.
        log.warning(
            "node.tool_calling.llm_fallback_escalating",
            investigation_id=investigation_id,
            hop=new_hop_count,
        )
        next_selection_dict = None
        escalation_reason = "low_confidence"

    # AUDT-02: state_change row — INVESTIGATING→TOOL_CALLING on first hop, EVALUATING→TOOL_CALLING thereafter
    state_from_for_audit = (
        InvestigationStatus.INVESTIGATING if hop == 0
        else InvestigationStatus.EVALUATING
    )
    write_audit_event(
        investigation_id=investigation_id,
        event_type="state_change",
        state_from=state_from_for_audit,
        state_to=InvestigationStatus.TOOL_CALLING,
        cost_usd_delta=cost_usd,
    )

    return {
        **state,
        "status": InvestigationStatus.TOOL_CALLING,
        "hop_count": new_hop_count,
        "accumulated_cost_usd": state.get("accumulated_cost_usd", 0.0) + cost_usd,
        "last_tool_result": result.model_dump(),
        "evidence_chain": new_evidence,
        "tool_selection": next_selection_dict,
        "escalation_reason": escalation_reason,
    }


def node_evaluating(state: AgentState) -> AgentState:
    """EVALUATING: empty-evidence guard, hard limits, then real LLM evaluation (LLM-02, LLM-03)."""
    from agent.limits import check_confidence, check_cost_cap, check_max_hops, check_timeout

    log.info(
        "node.evaluating",
        hops=state.get("hop_count"),
        evidence_entries=len(state.get("evidence_chain", [])),
    )

    # SM-07: empty-evidence guard — must fire before any other check
    if not state.get("evidence_chain"):
        payload = state.get("payload")
        investigation_id = payload.investigation_id if payload else None
        log.warning("node.evaluating.empty_evidence", investigation_id=investigation_id)
        write_audit_event(
            investigation_id=investigation_id,
            event_type="state_change",
            event_detail={"reason": "empty_evidence"},
            state_from=InvestigationStatus.EVALUATING,
            state_to=InvestigationStatus.ESCALATED,
        )
        return {
            **state,
            "status": InvestigationStatus.ESCALATED,
            "escalation_reason": "empty_evidence",
            "_pre_escalation_status": InvestigationStatus.EVALUATING,
        }

    # Hard limits before LLM (SM-08): max_hops, timeout, cost_cap
    for check_fn in (check_max_hops, check_timeout, check_cost_cap):
        reason = check_fn(state)
        if reason:
            return {
                **state,
                "status": InvestigationStatus.ESCALATED,
                "escalation_reason": reason,
                "_pre_escalation_status": InvestigationStatus.EVALUATING,
            }

    # Real LLM evaluation (LLM-02, LLM-03)
    user_msg = _build_evaluation_user_message(state)
    eval_result, cost_usd = call_llm(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_EVALUATION},
            {"role": "user", "content": user_msg},
        ],
        response_model=EvaluationOutput,
    )
    new_cost = state.get("accumulated_cost_usd", 0.0) + cost_usd
    state_with_eval = {
        **state,
        "accumulated_cost_usd": new_cost,
        "evaluation": eval_result.model_dump(),
    }

    # Re-check cost cap POST LLM call — the evaluation call itself may have pushed
    # total cost over the cap (CLAUDE.md: hard limits are unconditional).
    post_call_cost_reason = check_cost_cap(state_with_eval)
    if post_call_cost_reason:
        return {
            **state_with_eval,
            "status": InvestigationStatus.ESCALATED,
            "escalation_reason": post_call_cost_reason,
            "_pre_escalation_status": InvestigationStatus.EVALUATING,
        }

    # Confidence check runs AFTER the LLM call (SM-04)
    confidence_reason = check_confidence(state_with_eval)
    if confidence_reason:
        return {
            **state_with_eval,
            "status": InvestigationStatus.ESCALATED,
            "escalation_reason": confidence_reason,
            "_pre_escalation_status": InvestigationStatus.EVALUATING,
        }

    return {**state_with_eval, "status": InvestigationStatus.EVALUATING, "escalation_reason": None}


def node_resolved(state: AgentState) -> AgentState:
    """RESOLVED: builds ComplianceReport from evaluation + DB-sourced evidence chain,
    writes to compliance_reports (REPT-01), marks investigation complete.

    Evidence chain is sourced from tool_execution_log via build_evidence_chain
    (REPT-04/AUDT-04) — NOT from state.evidence_chain.
    """
    import datetime
    from models.schemas import ComplianceReport

    evaluation = state.get("evaluation") or {}
    payload = state.get("payload")
    investigation_id = payload.investigation_id if payload else "unknown"

    # REPT-04: evidence chain MUST come from tool_execution_log, not state
    evidence_chain = build_evidence_chain(investigation_id)

    # No verdict without evidence chain (CLAUDE.md contract)
    if not evidence_chain:
        log.warning("node.resolved.empty_evidence_chain_at_resolution", investigation_id=investigation_id)
        verdict = "inconclusive"
        recommendation = "escalate_to_compliance"
    else:
        verdict = evaluation.get("verdict", "inconclusive")
        recommendation = evaluation.get("recommendation", "monitor")

    report = ComplianceReport(
        investigation_id=investigation_id,
        txn_id=payload.txn_id if payload else "unknown",
        verdict=verdict,
        confidence=evaluation.get("confidence", 0.0),
        finding=evaluation.get("finding", ""),
        evidence_chain=evidence_chain,
        recommendation=recommendation,
        narrative=evaluation.get("narrative", ""),
        total_hops=state.get("hop_count", 0),
        total_cost_usd=state.get("accumulated_cost_usd", 0.0),
        resolved_at=datetime.datetime.now(datetime.timezone.utc),
    )

    # REPT-01: persist to compliance_reports. Non-fatal on failure.
    written = write_compliance_report(report)
    if not written:
        log.warning("node.resolved.db_write_failed", investigation_id=investigation_id)

    log.info(
        "node.resolved",
        investigation_id=report.investigation_id,
        verdict=report.verdict,
        confidence=report.confidence,
        total_hops=report.total_hops,
        total_cost_usd=report.total_cost_usd,
        db_written=written,
    )

    # AUDT-04: resolved row — investigation complete
    write_audit_event(
        investigation_id=investigation_id,
        event_type="resolved",
        event_detail={"verdict": report.verdict, "confidence": report.confidence},
        state_from=InvestigationStatus.EVALUATING,
        state_to=InvestigationStatus.RESOLVED,
    )

    return {**state, "status": InvestigationStatus.RESOLVED, "final_report": report.model_dump()}


def node_escalated(state: AgentState) -> AgentState:
    """ESCALATED: writes audit row + escalation_queue row, marks investigation escalated."""
    reason = state.get("escalation_reason")
    if reason is None:
        log.error("node.escalated.missing_reason", state_keys=list(state.keys()))

    payload = state.get("payload")
    investigation_id = payload.investigation_id if payload else "unknown"
    txn_id = payload.txn_id if payload else "unknown"
    evaluation = state.get("evaluation") or {}
    confidence = evaluation.get("confidence")

    log.info("node.escalated", investigation_id=investigation_id, reason=reason)

    # AUDT-05: append-only audit row on ESCALATED
    # _pre_escalation_status is set by node_evaluating before returning ESCALATED;
    # fallback to EVALUATING since node_escalated is always reached via that node.
    actual_state_from = state.get("_pre_escalation_status", InvestigationStatus.EVALUATING)
    write_audit_event(
        investigation_id=investigation_id,
        event_type="escalated",
        event_detail={"reason": reason},
        state_from=actual_state_from,
        state_to=InvestigationStatus.ESCALATED,
    )

    # ESC-01..06: write escalation_queue row with partial_report JSONB
    partial = build_partial_report(state)
    write_escalation(
        investigation_id=investigation_id,
        txn_id=txn_id,
        reason=reason or "low_confidence",
        confidence=confidence,
        partial_report=partial,
    )

    return {**state, "status": InvestigationStatus.ESCALATED}


# ── Routing logic (conditions wired in Plan 02) ──────────────────────────────


def route_after_evaluating(state: AgentState) -> str:
    """Routes based on escalation_reason (set by node_evaluating) and evaluation output."""
    from agent.limits import evaluate_routing

    # If node_evaluating already set a reason, route to escalated immediately
    if state.get("escalation_reason"):
        return "node_escalated"
    return evaluate_routing(state)


# ── Graph assembly ────────────────────────────────────────────────────────────


def build_graph() -> StateGraph:
    builder = StateGraph(AgentState)

    builder.add_node("node_investigating", node_investigating)
    builder.add_node("node_tool_calling", node_tool_calling)
    builder.add_node("node_evaluating", node_evaluating)
    builder.add_node("node_resolved", node_resolved)
    builder.add_node("node_escalated", node_escalated)

    # Entry point
    builder.set_entry_point("node_investigating")

    # Deterministic edges (per valid transition diagram)
    builder.add_edge("node_investigating", "node_tool_calling")
    builder.add_edge("node_tool_calling", "node_evaluating")

    # Conditional edge from EVALUATING (hard limit logic in Plan 02)
    builder.add_conditional_edges(
        "node_evaluating",
        route_after_evaluating,
        {
            "node_tool_calling": "node_tool_calling",
            "node_resolved": "node_resolved",
            "node_escalated": "node_escalated",
        },
    )

    # Terminal nodes
    builder.add_edge("node_resolved", END)
    builder.add_edge("node_escalated", END)

    return builder.compile()


# Module-level compiled graph — imported by test harness and API layer
AMLGraph = build_graph()
