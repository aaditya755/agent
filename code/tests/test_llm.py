"""Unit tests for LLM caching, retries, QuotaExhausted, and usage ledger.

Uses fake/mock client to ensure 100% offline determinism without network access.
"""

from dataclasses import dataclass
import json
from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch

from google.genai import errors

import config
import llm
import usage


@dataclass
class FakeUsageMetadata:
    prompt_token_count: int = 15
    candidates_token_count: int = 25


@dataclass
class FakeResponse:
    text: str = "Fake generated response"
    function_calls: list = None
    usage_metadata: FakeUsageMetadata = None

    def __post_init__(self):
        if self.function_calls is None:
            self.function_calls = []
        if self.usage_metadata is None:
            self.usage_metadata = FakeUsageMetadata()


class FakeModels:
    def __init__(self, response=None, fail_count=0, error_code=429):
        self.call_count = 0
        self.response = response or FakeResponse()
        self.fail_count = fail_count
        self.error_code = error_code

    def generate_content(self, model, contents, config):
        self.call_count += 1
        if self.call_count <= self.fail_count:
            if self.error_code == 429:
                err = errors.APIError(code=429, response_json={"error": {"message": "Resource has been exhausted"}})
                raise err
            else:
                err = errors.ServerError(code=503, response_json={"error": {"message": "Service unavailable"}})
                raise err
        return self.response


class FakeClient:
    def __init__(self, response=None, fail_count=0, error_code=429):
        self.models = FakeModels(response=response, fail_count=fail_count, error_code=error_code)


def test_cache_miss_then_hit(tmp_path):
    """Test that first call hits API and caches, second call hits cache without calling API."""
    fake_client = FakeClient(response=FakeResponse(text="Test answer"))
    cache_dir = tmp_path / "llm_cache"
    ledger_path = tmp_path / "usage.jsonl"

    # Call 1: Cache miss
    res1 = llm.generate(
        prompt="Hello world",
        model="fake-gemini",
        purpose="test-miss",
        client=fake_client,
        cache_dir=cache_dir,
        ledger_path=ledger_path,
    )
    assert res1.text == "Test answer"
    assert res1.cache_hit is False
    assert fake_client.models.call_count == 1
    assert res1.input_tokens == 15
    assert res1.output_tokens == 25

    # Verify cache file exists
    cache_files = list(cache_dir.glob("*.json"))
    assert len(cache_files) == 1

    # Call 2: Cache hit
    res2 = llm.generate(
        prompt="Hello world",
        model="fake-gemini",
        purpose="test-hit",
        client=fake_client,
        cache_dir=cache_dir,
        ledger_path=ledger_path,
    )
    assert res2.text == "Test answer"
    assert res2.cache_hit is True
    # Fake client should NOT have been called again!
    assert fake_client.models.call_count == 1

    # Check usage ledger has 2 records
    lines = [json.loads(l) for l in ledger_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2
    assert lines[0]["cache_hit"] is False
    assert lines[1]["cache_hit"] is True


def test_cache_key_differentiation():
    """Verify different prompts/models/images create distinct cache keys."""
    k1 = llm.compute_cache_key("model-a", "prompt 1")
    k2 = llm.compute_cache_key("model-a", "prompt 2")
    k3 = llm.compute_cache_key("model-b", "prompt 1")
    k4 = llm.compute_cache_key("model-a", "prompt 1", image_bytes=b"png1")
    k5 = llm.compute_cache_key("model-a", "prompt 1", image_bytes=b"png2")

    keys = {k1, k2, k3, k4, k5}
    assert len(keys) == 5


def test_retry_on_429_success(tmp_path):
    """Test retry with backoff when 429 fails once then succeeds."""
    fake_client = FakeClient(fail_count=1, error_code=429)
    cache_dir = tmp_path / "llm_cache"
    ledger_path = tmp_path / "usage.jsonl"

    with patch("time.sleep", return_value=None):
        res = llm.generate(
            prompt="Retry test",
            model="fake-gemini",
            purpose="test-retry",
            client=fake_client,
            cache_dir=cache_dir,
            ledger_path=ledger_path,
            max_retries=2,
        )
    assert res.text == "Fake generated response"
    assert fake_client.models.call_count == 2


def test_quota_exhausted_raises(tmp_path):
    """Test typed QuotaExhausted exception is raised when 429 retries are exhausted."""
    fake_client = FakeClient(fail_count=5, error_code=429)
    cache_dir = tmp_path / "llm_cache"
    ledger_path = tmp_path / "usage.jsonl"

    with patch("time.sleep", return_value=None):
        with pytest.raises(llm.QuotaExhausted) as exc_info:
            llm.generate(
                prompt="Exhausted test",
                model="fake-gemini",
                purpose="test-exhaust",
                client=fake_client,
                cache_dir=cache_dir,
                ledger_path=ledger_path,
                max_retries=2,
            )
    assert "rate limit or quota exceeded" in str(exc_info.value)


def test_usage_summary(tmp_path):
    """Test usage ledger aggregation and reporting."""
    ledger_path = tmp_path / "usage.jsonl"
    usage.log_usage("p1", "model-alpha", 100, 50, False, 120.0, ledger_path=ledger_path)
    usage.log_usage("p2", "model-alpha", 100, 50, True, 2.0, ledger_path=ledger_path)
    usage.log_usage("p3", "model-beta", 200, 80, False, 300.0, ledger_path=ledger_path)

    summary = usage.get_usage_summary(ledger_path=ledger_path)
    assert summary["overall"]["calls"] == 3
    assert summary["overall"]["input_tokens"] == 400
    assert summary["overall"]["output_tokens"] == 180
    assert summary["overall"]["total_tokens"] == 580
    assert summary["overall"]["cache_hits"] == 1
    assert summary["overall"]["cost"] == "unknown (prices not set)"

    assert summary["per_model"]["model-alpha"]["calls"] == 2
    assert summary["per_model"]["model-alpha"]["cache_hits"] == 1
    assert summary["per_model"]["model-beta"]["calls"] == 1


def test_validator_unlinked_complete_exemption():
    """Verify validator exempts COMPLETE cases with 0 steps when flagged account_unlinked."""
    import validator

    # Without exemption: empty steps on COMPLETE fails
    errors_normal = validator.validate_steps("case_999", "COMPLETE", [], account_unlinked=False)
    assert len(errors_normal) == 1
    assert "cannot have empty steps_json" in errors_normal[0]

    # With exemption: empty steps on COMPLETE passes
    errors_exempt = validator.validate_steps("case_999", "COMPLETE", [], account_unlinked=True)
    assert len(errors_exempt) == 0

    # Non-COMPLETE statuses (like INCOMPLETE) still fail even if account_unlinked
    errors_incomplete = validator.validate_steps("case_999", "INCOMPLETE", [], account_unlinked=True)
    assert len(errors_incomplete) == 1
    assert "cannot have empty steps_json" in errors_incomplete[0]


def test_llm_models_requires_only_api_key(monkeypatch):
    """Verify llm-models command only requires GEMINI_API_KEY, not model names."""
    import main

    # Ensure model names are not required
    monkeypatch.setenv("GEMINI_API_KEY", "dummy_key_for_test")
    monkeypatch.delenv("GEMINI_MODEL_AGENT", raising=False)
    monkeypatch.delenv("GEMINI_MODEL_ESCALATION", raising=False)
    monkeypatch.delenv("GEMINI_MODEL_VISION", raising=False)

    fake_model = MagicMock()
    fake_model.name = "models/gemini-test"
    fake_model.supported_actions = ["generateContent"]

    fake_client = MagicMock()
    fake_client.models.list.return_value = [fake_model]

    with patch("llm.get_client", return_value=fake_client):
        # Should not raise any missing env var error for model names
        main.cmd_llm_models()


def test_cmd_llm_ratecheck_mock(monkeypatch, capsys):
    """Verify cmd_llm_ratecheck runs and prints summary with mock LLM calls."""
    import main

    monkeypatch.setenv("GEMINI_API_KEY", "dummy_key")
    monkeypatch.setenv("GEMINI_MODEL_AGENT", "gemini-test-agent")
    monkeypatch.setenv("GEMINI_MODEL_ESCALATION", "gemini-test-esc")

    mock_resp = llm.LLMResult(
        text="PONG",
        input_tokens=5,
        output_tokens=2,
        cache_hit=False,
        latency_ms=10.0,
    )

    with patch("llm.generate", return_value=mock_resp), \
         patch("usage.print_usage_summary", return_value=None):
        main.cmd_llm_ratecheck()

    captured = capsys.readouterr().out
    assert "=== BATCH 1: 10 Sequential Calls" in captured
    assert "=== BATCH 2: 4 Parallel Calls" in captured
    assert "=== BATCH 3: 2 Sequential Calls" in captured
    assert "=== RATECHECK SUMMARY ===" in captured
    assert "Total Calls: 16" in captured
    assert "Successes: 16" in captured

