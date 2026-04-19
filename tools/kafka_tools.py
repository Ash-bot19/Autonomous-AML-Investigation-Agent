"""
tools/kafka_tools.py — Kafka-backed AML investigation tools.

Tool:
  - kafka_lag_check: returns consumer group lag for the AML investigation agent.

is_pipeline_delay=True when lag > 0 — indicates the agent may be processing a
backlog, so the flagged transaction might not represent new suspicious activity.

KafkaAdminClient timeout: 5000ms (T-03-03-04 mitigation).

Import note: kafka imports are lazy (inside the function) to avoid the project's
kafka/ directory shadowing the installed kafka-python-ng package at module level.
Tests inject KafkaAdminClient / KafkaConsumer via tools.kafka_tools module attrs.

Never raises — returns ToolResult(success=False) on any exception.
"""
from __future__ import annotations

import importlib
import os
import structlog

from models.schemas import ToolResult, KafkaLagInput

log = structlog.get_logger()

# Module-level references — None by default, populated lazily on first call.
# Tests can inject mocks by setting tools.kafka_tools.KafkaAdminClient = MockClass.
KafkaAdminClient = None  # type: ignore[assignment]
KafkaConsumer = None  # type: ignore[assignment]


def _ensure_kafka_imports() -> bool:
    """
    Load KafkaAdminClient and KafkaConsumer from kafka-python-ng into this
    module's namespace. Returns True if successful, False otherwise.

    Uses importlib to bypass the project's kafka/ directory that would shadow
    the installed library if we used a top-level `from kafka import ...`.
    """
    global KafkaAdminClient, KafkaConsumer  # noqa: PLW0603
    if KafkaAdminClient is not None:
        return True
    try:
        kafka_admin = importlib.import_module("kafka.admin")
        kafka_root = importlib.import_module("kafka")
        KafkaAdminClient = kafka_admin.KafkaAdminClient
        KafkaConsumer = kafka_root.KafkaConsumer
        return True
    except Exception as exc:
        log.warning("kafka_tools.import_failed", error=str(exc))
        return False


# ── kafka_lag_check ──────────────────────────────────────────────────────────


def kafka_lag_check(inp: KafkaLagInput) -> ToolResult:
    """
    Returns consumer group lag for the AML investigation agent.

    Computes lag = sum(end_offset - committed_offset) across all partitions.
    is_pipeline_delay=True when total_lag > 0.

    On any exception (broker unavailable, timeout, import error) returns
    ToolResult(success=False).

    Data shape:
      {"consumer_group": str, "lag": int, "is_pipeline_delay": bool}
    """
    try:
        if not _ensure_kafka_imports():
            return ToolResult(
                success=False,
                tool_name="kafka_lag_check",
                error="kafka-python-ng not importable",
            )

        bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
        group = os.environ.get("KAFKA_CONSUMER_GROUP", "aml-investigation-agent")

        admin = KafkaAdminClient(
            bootstrap_servers=bootstrap,
            client_id="aml-lag-checker",
            request_timeout_ms=5000,
        )
        try:
            committed_offsets = admin.list_consumer_group_offsets(group)
        finally:
            admin.close()

        total_lag = 0
        if committed_offsets:
            tmp_consumer = KafkaConsumer(bootstrap_servers=bootstrap)
            try:
                end_offsets = tmp_consumer.end_offsets(list(committed_offsets.keys()))
                for tp, offset_meta in committed_offsets.items():
                    committed_val = offset_meta.offset if offset_meta else 0
                    end_val = end_offsets.get(tp, committed_val)
                    total_lag += max(0, end_val - committed_val)
            finally:
                tmp_consumer.close()

        log.info(
            "kafka_lag_check.ok",
            consumer_group=group,
            lag=total_lag,
            is_pipeline_delay=total_lag > 0,
        )
        return ToolResult(
            success=True,
            tool_name="kafka_lag_check",
            data={
                "consumer_group": group,
                "lag": int(total_lag),
                "is_pipeline_delay": total_lag > 0,
            },
        )

    except Exception as exc:
        log.error("kafka_lag_check.error", error=str(exc))
        return ToolResult(
            success=False,
            tool_name="kafka_lag_check",
            error=str(exc),
        )
