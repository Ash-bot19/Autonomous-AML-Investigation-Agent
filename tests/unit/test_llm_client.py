"""Unit tests for agent/llm_client.py — all OpenAI calls are mocked; no network access."""
from unittest.mock import MagicMock, patch

import pytest

from agent.llm_client import call_llm, PRICE_INPUT_PER_TOKEN, PRICE_OUTPUT_PER_TOKEN, LLM_MODEL
from models.schemas import EvaluationOutput, ToolSelectionOutput


def _make_completion(parsed_obj, prompt_tokens: int = 100, completion_tokens: int = 50):
    """Build a minimal mock of the OpenAI completion response."""
    completion = MagicMock()
    completion.choices[0].message.parsed = parsed_obj
    completion.usage.prompt_tokens = prompt_tokens
    completion.usage.completion_tokens = completion_tokens
    return completion


def _mock_client(completion):
    """Return a mock OpenAI client whose .beta.chat.completions.parse() returns completion."""
    client = MagicMock()
    client.beta.chat.completions.parse.return_value = completion
    return client


MESSAGES = [{"role": "user", "content": "investigate txn ABC"}]


class TestCallLlmToolSelectionSuccess:
    def test_returns_tool_selection_output_and_positive_cost(self):
        parsed = ToolSelectionOutput(
            tool_name="velocity_check",
            tool_input={"account_id": "ACC-001"},
            reasoning="check velocity first",
        )
        completion = _make_completion(parsed, prompt_tokens=200, completion_tokens=80)
        with patch("agent.llm_client._get_openai_client", return_value=_mock_client(completion)):
            result, cost = call_llm(MESSAGES, ToolSelectionOutput)
        assert isinstance(result, ToolSelectionOutput)
        assert result.tool_name == "velocity_check"
        assert cost > 0.0


class TestCallLlmEvaluationSuccess:
    def test_returns_evaluation_output_and_positive_cost(self):
        parsed = EvaluationOutput(
            confidence=0.88,
            verdict="suspicious",
            finding="high velocity detected",
            recommendation="escalate_to_compliance",
            narrative="23 transactions in 6 hours.",
            should_continue=False,
        )
        completion = _make_completion(parsed, prompt_tokens=300, completion_tokens=120)
        with patch("agent.llm_client._get_openai_client", return_value=_mock_client(completion)):
            result, cost = call_llm(MESSAGES, EvaluationOutput)
        assert isinstance(result, EvaluationOutput)
        assert result.confidence == pytest.approx(0.88)
        assert result.should_continue is False
        assert cost > 0.0


class TestCostComputationExact:
    def test_1000_prompt_500_completion(self):
        """(1000 * 0.15/1e6) + (500 * 0.60/1e6) = 0.00015 + 0.00030 = 0.00045"""
        parsed = ToolSelectionOutput(
            tool_name="watchlist_lookup",
            tool_input={"entity_name": "ACME"},
            reasoning="check watchlist",
        )
        completion = _make_completion(parsed, prompt_tokens=1000, completion_tokens=500)
        with patch("agent.llm_client._get_openai_client", return_value=_mock_client(completion)):
            _, cost = call_llm(MESSAGES, ToolSelectionOutput)
        assert cost == pytest.approx(0.00045, abs=1e-9)

    def test_price_constants_match_spec(self):
        assert PRICE_INPUT_PER_TOKEN == pytest.approx(0.15 / 1_000_000)
        assert PRICE_OUTPUT_PER_TOKEN == pytest.approx(0.60 / 1_000_000)


class TestOpenAIErrorReturnsFallback:
    def test_exception_returns_fallback_evaluation(self):
        with patch("agent.llm_client._get_openai_client", side_effect=Exception("rate limited")):
            result, cost = call_llm(MESSAGES, EvaluationOutput)
        assert isinstance(result, EvaluationOutput)
        assert result.verdict == "inconclusive"
        assert result.confidence == 0.0
        assert result.should_continue is False
        assert result.recommendation == "escalate_to_compliance"
        assert cost == 0.0

    def test_runtime_error_returns_fallback(self):
        with patch("agent.llm_client._get_openai_client", side_effect=RuntimeError("network down")):
            result, cost = call_llm(MESSAGES, ToolSelectionOutput)
        assert isinstance(result, EvaluationOutput)
        assert result.confidence == 0.0
        assert cost == 0.0


class TestParsedNoneReturnsFallback:
    def test_none_parsed_returns_fallback(self):
        completion = _make_completion(parsed_obj=None)
        with patch("agent.llm_client._get_openai_client", return_value=_mock_client(completion)):
            result, cost = call_llm(MESSAGES, EvaluationOutput)
        assert isinstance(result, EvaluationOutput)
        assert result.verdict == "inconclusive"
        assert result.confidence == 0.0
        assert result.should_continue is False
        assert cost == 0.0


class TestCallLlmNeverRaises:
    def test_no_exception_propagated(self):
        with patch("agent.llm_client._get_openai_client", side_effect=RuntimeError("boom")):
            try:
                call_llm(MESSAGES, EvaluationOutput)
            except Exception:
                pytest.fail("call_llm raised an exception — it must never raise")


class TestLlmModelConstant:
    def test_model_is_gpt4o_mini(self):
        assert LLM_MODEL == "gpt-4o-mini"
