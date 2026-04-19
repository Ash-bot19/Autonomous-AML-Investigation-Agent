"""
agent/llm_client.py — gpt-4o-mini wrapper for tool selection and evidence evaluation.

Single entry point: call_llm(messages, response_model) -> (parsed_instance, cost_usd).
Never raises. On any OpenAI error or parse failure, returns a fallback
EvaluationOutput-style result that routes to ESCALATED. Cost cap enforcement
is the state machine's job (agent/limits.py) — this module only tracks cost.

Callers are responsible for not embedding raw PII in message content (T-04-01-01).
This module forwards messages verbatim; it never logs message content, only token counts.
"""
from __future__ import annotations

from typing import Type, TypeVar

import structlog
from pydantic import BaseModel

from models.schemas import EvaluationOutput

log = structlog.get_logger()

# gpt-4o-mini pricing (USD per token) — locked in 04-CONTEXT.md decisions
PRICE_INPUT_PER_TOKEN: float = 0.15 / 1_000_000   # $0.15 per 1M prompt tokens
PRICE_OUTPUT_PER_TOKEN: float = 0.60 / 1_000_000  # $0.60 per 1M completion tokens

LLM_MODEL: str = "gpt-4o-mini"

T = TypeVar("T", bound=BaseModel)

_client = None


def _get_openai_client():
    """Lazy-init OpenAI client. Test code patches this to inject mocks."""
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI()
    return _client


def _compute_cost_usd(prompt_tokens: int, completion_tokens: int) -> float:
    return (
        prompt_tokens * PRICE_INPUT_PER_TOKEN
        + completion_tokens * PRICE_OUTPUT_PER_TOKEN
    )


def _fallback_evaluation(reason: str) -> EvaluationOutput:
    """
    Returned when the LLM call fails or returns invalid output.
    confidence=0.0 and should_continue=False guarantee the state machine
    routes to ESCALATED via the low_confidence check in limits.py.
    """
    return EvaluationOutput(
        confidence=0.0,
        verdict="inconclusive",
        finding=f"LLM unavailable: {reason}",
        recommendation="escalate_to_compliance",
        narrative=(
            f"Investigation could not be completed because the LLM evaluation step "
            f"failed ({reason}). Escalating to a human analyst."
        ),
        should_continue=False,
    )


def call_llm(
    messages: list[dict[str, str]],
    response_model: Type[T],
) -> tuple[T | EvaluationOutput, float]:
    """
    Call gpt-4o-mini with structured output and return (parsed, cost_usd).

    Args:
        messages: OpenAI chat messages list ({"role": ..., "content": ...}).
        response_model: A Pydantic BaseModel subclass — ToolSelectionOutput
                        or EvaluationOutput.

    Returns:
        (parsed_instance, cost_usd) on success.
        (_fallback_evaluation(reason), 0.0) on any failure — never raises.
    """
    try:
        client = _get_openai_client()
        completion = client.beta.chat.completions.parse(
            model=LLM_MODEL,
            messages=messages,
            response_format=response_model,
        )
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            log.warning("llm.parse_returned_none", model_name=response_model.__name__)
            return _fallback_evaluation("parsed message was None"), 0.0

        usage = completion.usage
        if usage is None:
            log.warning("llm.usage_missing", model_name=response_model.__name__)
            return _fallback_evaluation("usage metadata missing from response"), 0.0

        cost = _compute_cost_usd(usage.prompt_tokens, usage.completion_tokens)
        log.info(
            "llm.call_ok",
            model=LLM_MODEL,
            response_model=response_model.__name__,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=round(cost, 6),
        )
        return parsed, cost

    except Exception as exc:
        log.error("llm.call_failed", error=str(exc), error_type=type(exc).__name__)
        return _fallback_evaluation(str(exc)[:120]), 0.0
