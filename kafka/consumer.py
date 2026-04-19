"""Kafka consumer for AML flagged transaction events.

Subscribes to aml-flagged-transactions topic, deserialises InvestigationPayload
from JSON, calls run_investigation. On deserialisation or validation error: logs
and commits offset (do not block the consumer).

Import note: KafkaConsumer is loaded lazily via importlib to bypass the project's
kafka/ directory that shadows kafka-python-ng at module level. Tests can inject a
mock by setting kafka.consumer.KafkaConsumer = MockClass before calling run_consumer.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import structlog

from agent.runner import run_investigation
from models.schemas import InvestigationPayload

log = structlog.get_logger()

# Module-level reference — None by default, populated lazily by _ensure_kafka_imports().
# Tests can inject mocks by setting kafka.consumer.KafkaConsumer = MockClass.
KafkaConsumer = None  # type: ignore[assignment]


def _ensure_kafka_imports() -> bool:
    """Load KafkaConsumer from kafka-python-ng into this module's namespace.

    Temporarily removes the local kafka/ directory and project ROOT from sys.path
    so that importlib resolves to the installed kafka-python-ng package rather than
    the empty local kafka/__init__.py. Restores sys.path afterward.
    Returns True if successful, False otherwise.
    """
    global KafkaConsumer  # noqa: PLW0603
    if KafkaConsumer is not None:
        return True
    try:
        root = Path(__file__).resolve().parent.parent
        shadow_paths = {str(root / "kafka"), str(root), str(Path(__file__).parent)}
        orig_path = sys.path[:]
        sys.path = [p for p in sys.path if p not in shadow_paths]
        # Evict any already-loaded local kafka module so importlib re-resolves
        for key in [k for k in sys.modules if k == "kafka" or k.startswith("kafka.")]:
            del sys.modules[key]
        try:
            kafka_root = importlib.import_module("kafka")
            KafkaConsumer = kafka_root.KafkaConsumer  # type: ignore[assignment]
            return True
        finally:
            sys.path = orig_path
    except Exception as exc:
        log.warning("kafka.consumer.import_failed", error=str(exc))
        return False


def _build_consumer() -> Any:
    """Build and return a configured KafkaConsumer instance."""
    if not _ensure_kafka_imports():
        raise RuntimeError("kafka-python-ng KafkaConsumer not importable")

    bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    group_id = os.environ.get("KAFKA_CONSUMER_GROUP", "aml-investigation-agent")
    topic = os.environ.get("KAFKA_FLAGGED_TOPIC", "aml-flagged-transactions")
    consumer = KafkaConsumer(
        topic,
        bootstrap_servers=bootstrap,
        group_id=group_id,
        auto_offset_reset="earliest",
        enable_auto_commit=False,
        value_deserializer=lambda v: v,  # raw bytes; we parse manually
    )
    log.info("kafka.consumer.started", topic=topic, group_id=group_id, bootstrap=bootstrap)
    return consumer


def _process_message(raw: bytes, dispatch_fn=run_investigation) -> None:
    """Process a single Kafka message. Extracted for testability.

    On deserialisation error: logs kafka.consumer.deserialize_error and returns.
    On validation error: logs kafka.consumer.payload_validation_error and returns.
    On dispatch exception: logs kafka.consumer.run_investigation_error and returns.
    Never raises — caller (run_consumer) commits offset after each call.
    """
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        log.error(
            "kafka.consumer.deserialize_error",
            error=str(exc),
            raw_preview=str(raw[:100]),
        )
        return

    try:
        payload = InvestigationPayload(**data)
    except Exception as exc:
        log.error(
            "kafka.consumer.payload_validation_error",
            error=str(exc),
            data=data,
        )
        return

    try:
        dispatch_fn(payload)
    except Exception as exc:
        log.error(
            "kafka.consumer.run_investigation_error",
            txn_id=data.get("txn_id"),
            error=str(exc),
        )


def run_consumer() -> None:
    """Block forever, consuming messages and dispatching investigations.

    Intended to run as a subprocess managed by scripts/start.py.
    """
    consumer = _build_consumer()
    for message in consumer:
        _process_message(message.value)
        consumer.commit()


if __name__ == "__main__":
    run_consumer()
