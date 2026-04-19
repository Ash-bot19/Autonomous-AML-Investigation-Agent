"""Investigation runner — single entrypoint for Kafka consumer and FastAPI trigger.

Applies ML score routing (TRIG-03/04/05), acquires per-txn_id Redis mutex (TRIG-06),
then invokes the LangGraph AMLGraph. Never raises — exceptions are caught, logged, and
mutex is always released.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import redis
import structlog
from prometheus_client import Counter, Histogram

from agent.graph import AMLGraph
from agent.state import InvestigationStatus
from models.schemas import InvestigationPayload

log = structlog.get_logger()

# ── Prometheus metrics ────────────────────────────────────────────────────────

investigation_count_total = Counter(
    "aml_investigation_count_total",
    "Total number of AML investigations started (excluding log_only and already_investigating)",
)

escalation_count_total = Counter(
    "aml_escalation_count_total",
    "Total number of AML investigations that exited ESCALATED",
)

investigation_cost_usd = Histogram(
    "aml_investigation_cost_usd",
    "Cost in USD per completed investigation (RESOLVED or ESCALATED)",
    buckets=[0.005, 0.01, 0.015, 0.02, 0.025, 0.03, 0.035, 0.04, 0.045, 0.05],
)

investigation_hops = Histogram(
    "aml_investigation_hops",
    "Number of tool hops per completed investigation",
    buckets=[1, 2, 3, 4],
)

# ── Redis client (lazy init) ──────────────────────────────────────────────────

_redis_client: Optional[redis.Redis] = None
_redis_init_attempted: bool = False


def _get_redis() -> Optional[redis.Redis]:
    """Return a connected Redis client, or None if Redis is unavailable.

    Attempts connection exactly once; subsequent calls fast-fail without a network round-trip.
    """
    global _redis_client, _redis_init_attempted
    if _redis_client is not None:
        return _redis_client
    if _redis_init_attempted:
        return None
    try:
        _redis_client = redis.Redis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            decode_responses=True,
        )
        _redis_client.ping()
        return _redis_client
    except Exception as exc:
        log.warning("runner.redis_unavailable", error=str(exc))
        _redis_client = None
        return None
    finally:
        _redis_init_attempted = True


# ── ML routing helpers ────────────────────────────────────────────────────────


def _should_investigate(payload: InvestigationPayload) -> bool:
    """Return True if the investigation should proceed based on ML score routing.

    Rules (TRIG-03/04/05):
    - Only applies when trigger_type == "ml_score"
    - risk_score < 0.55: unexpected — log warning (possible upstream misconfiguration)
    - risk_score 0.55–0.75: log only, return False
    - risk_score > 0.75: proceed
    - rule_engine and both: always proceed regardless of score
    """
    if payload.trigger_type != "ml_score":
        return True
    score = payload.risk_score
    if score is None:
        return True
    if score < 0.55:
        log.warning(
            "aml.trigger.unexpected_low_score",
            txn_id=payload.txn_id,
            risk_score=score,
            reason="score_below_monitoring_floor_possible_upstream_misconfiguration",
        )
        return False
    if score <= 0.75:
        log.info(
            "aml.trigger.log_only",
            txn_id=payload.txn_id,
            risk_score=score,
            reason="score_below_threshold",
        )
        return False
    return True


def _apply_priority_flag(payload: InvestigationPayload) -> InvestigationPayload:
    """Mutate trigger_detail to include 'high_priority' for scores > 0.90 (TRIG-05).

    Returns a new InvestigationPayload (immutable update via model_copy).
    Only applies when trigger_type == 'ml_score'.
    """
    if (
        payload.trigger_type == "ml_score"
        and payload.risk_score is not None
        and payload.risk_score > 0.90
    ):
        updated_detail = payload.trigger_detail + " [high_priority]"
        return payload.model_copy(update={"trigger_detail": updated_detail})
    return payload


# ── Main entrypoint ───────────────────────────────────────────────────────────


def run_investigation(payload: InvestigationPayload) -> dict[str, Any]:
    """Run an AML investigation for the given payload.

    Returns:
        dict with keys: investigation_id, txn_id, status
        On already_investigating: {"status": "already_investigating", "txn_id": txn_id}
        On log_only: {"status": "log_only", "txn_id": txn_id}
        On completion: {"status": "RESOLVED" | "ESCALATED", "investigation_id": ..., "txn_id": ...}
        On error: {"status": "error", "investigation_id": ..., "txn_id": ..., "error": str}
    """
    # TRIG-03/04: ML score routing
    if not _should_investigate(payload):
        return {"status": "log_only", "txn_id": payload.txn_id}

    # TRIG-05: pre-flag high priority
    payload = _apply_priority_flag(payload)

    # TRIG-06: Redis mutex — SET mutex:investigation:{txn_id} 1 NX EX 300
    mutex_key = f"mutex:investigation:{payload.txn_id}"
    r = _get_redis()
    mutex_acquired = False

    if r is not None:
        acquired = r.set(mutex_key, payload.investigation_id, nx=True, ex=300)
        if not acquired:
            log.warning(
                "runner.duplicate_investigation_blocked",
                txn_id=payload.txn_id,
                mutex_key=mutex_key,
            )
            return {"status": "already_investigating", "txn_id": payload.txn_id}
        mutex_acquired = True
    else:
        log.warning("runner.mutex_skipped_redis_unavailable", txn_id=payload.txn_id)

    investigation_count_total.inc()  # OBS-01: count every investigation that actually runs

    # Build initial AgentState and invoke graph
    initial_state = {
        "payload": payload,
        "status": InvestigationStatus.IDLE,
        "hop_count": 0,
        "accumulated_cost_usd": 0.0,
        "started_at": None,
        "tool_selection": None,
        "last_tool_result": None,
        "evidence_chain": [],
        "evaluation": None,
        "escalation_reason": None,
        "final_report": None,
    }

    try:
        result = AMLGraph.invoke(initial_state)
        final_status = result.get("status", "unknown")
        # OBS-02: count escalations
        if final_status == InvestigationStatus.ESCALATED:
            escalation_count_total.inc()
        # OBS-03/04: record cost and hops from final report if available
        final_report = result.get("final_report")
        if final_report is not None:
            cost = getattr(final_report, "total_cost_usd", None)
            hops = getattr(final_report, "total_hops", None)
            if cost is not None:
                investigation_cost_usd.observe(float(cost))
            if hops is not None:
                investigation_hops.observe(float(hops))
        else:
            # ESCALATED path: get cost from accumulated_cost_usd in state
            cost = result.get("accumulated_cost_usd", 0.0)
            hops = result.get("hop_count", 0)
            investigation_cost_usd.observe(float(cost or 0.0))
            investigation_hops.observe(float(hops or 0))
        log.info(
            "runner.investigation_complete",
            investigation_id=payload.investigation_id,
            txn_id=payload.txn_id,
            status=final_status,
        )
        return {
            "investigation_id": payload.investigation_id,
            "txn_id": payload.txn_id,
            "status": final_status,
        }
    except Exception as exc:
        log.error(
            "runner.graph_exception",
            investigation_id=payload.investigation_id,
            txn_id=payload.txn_id,
            error=str(exc),
        )
        return {
            "investigation_id": payload.investigation_id,
            "txn_id": payload.txn_id,
            "status": "error",
            "error": str(exc),
        }
    finally:
        # TRIG-06: always release mutex (success, escalation, or exception)
        if r is not None and mutex_acquired:
            try:
                r.delete(mutex_key)
            except Exception as del_exc:
                log.error(
                    "runner.mutex_delete_failed",
                    mutex_key=mutex_key,
                    error=str(del_exc),
                )
