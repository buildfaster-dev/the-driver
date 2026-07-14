import json
from unittest.mock import MagicMock, patch

import anthropic
import click
import httpx
import pytest

from vetter.router import (
    MAX_ATTEMPTS,
    AnthropicProvider,
    FatalProviderError,
    ProviderResponse,
    TransientProviderError,
    _call_with_retry,
    complete,
)


def _status_error(cls, status_code):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code, request=request)
    return cls("boom", response=response, body=None)


def _read_log(log_path):
    return [json.loads(line) for line in log_path.read_text().splitlines()]


def _make_client_raising(exc):
    client = MagicMock()
    client.messages.create.side_effect = exc
    return client


CALL_ARGS = {
    "system": "system prompt",
    "user_content": "user content",
    "model_id": "claude-sonnet-4-6",
    "max_tokens": 4096,
    "temperature": 0.0,
}


class TestAnthropicProviderErrorTranslation:
    """Inject real SDK exception classes; assert the router-level category."""

    def _provider_with(self, exc):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("vetter.router.anthropic.Anthropic") as mock_class:
                mock_class.return_value = _make_client_raising(exc)
                provider = AnthropicProvider()
        return provider

    @pytest.mark.parametrize("exc_class,status", [
        (anthropic.RateLimitError, 429),
        (anthropic.InternalServerError, 500),
        (anthropic.InternalServerError, 529),
    ])
    def test_transient_status_errors(self, exc_class, status):
        provider = self._provider_with(_status_error(exc_class, status))
        with pytest.raises(TransientProviderError):
            provider.complete(**CALL_ARGS)

    def test_connection_error_is_transient(self):
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        provider = self._provider_with(anthropic.APIConnectionError(request=request))
        with pytest.raises(TransientProviderError):
            provider.complete(**CALL_ARGS)

    def test_timeout_is_transient(self):
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        provider = self._provider_with(anthropic.APITimeoutError(request=request))
        with pytest.raises(TransientProviderError):
            provider.complete(**CALL_ARGS)

    @pytest.mark.parametrize("exc_class,status", [
        (anthropic.AuthenticationError, 401),
        (anthropic.PermissionDeniedError, 403),
        (anthropic.NotFoundError, 404),
        (anthropic.BadRequestError, 400),
    ])
    def test_fatal_status_errors(self, exc_class, status):
        provider = self._provider_with(_status_error(exc_class, status))
        with pytest.raises(FatalProviderError):
            provider.complete(**CALL_ARGS)

    def test_missing_api_key_is_fatal(self):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(FatalProviderError, match="ANTHROPIC_API_KEY"):
                AnthropicProvider()


class TestRetryPolicy:
    """Effects on a fake provider: how many times it was called, what escalated."""

    def _fake_provider(self, side_effect):
        provider = MagicMock()
        provider.name = "anthropic"
        provider.complete.side_effect = side_effect
        return provider

    @patch("vetter.router.time.sleep")
    def test_transient_then_success_retries_and_recovers(self, mock_sleep):
        ok = ProviderResponse(text="ok", input_tokens=10, output_tokens=5)
        provider = self._fake_provider([TransientProviderError("429"), ok])

        response = _call_with_retry(provider, "s", "u", "claude-sonnet-4-6", 4096, 0.0)

        assert response.text == "ok"
        assert provider.complete.call_count == 2
        assert mock_sleep.call_count == 1

    @patch("vetter.router.time.sleep")
    def test_persistent_transient_exhausts_attempts(self, mock_sleep):
        provider = self._fake_provider(TransientProviderError("500"))

        with pytest.raises(TransientProviderError):
            _call_with_retry(provider, "s", "u", "claude-sonnet-4-6", 4096, 0.0)

        assert provider.complete.call_count == MAX_ATTEMPTS
        assert mock_sleep.call_count == MAX_ATTEMPTS - 1

    @patch("vetter.router.time.sleep")
    def test_fatal_fails_fast_without_retry(self, mock_sleep):
        provider = self._fake_provider(FatalProviderError("401"))

        with pytest.raises(FatalProviderError):
            _call_with_retry(provider, "s", "u", "claude-sonnet-4-6", 4096, 0.0)

        assert provider.complete.call_count == 1
        mock_sleep.assert_not_called()


class TestCallLog:
    """The JSONL is the router's testimony; the mocks are independent evidence.

    Every assertion about the log is cross-checked against mock effects.
    """

    @patch("vetter.router.time.sleep")
    def test_retry_run_logs_one_record_per_attempt(self, mock_sleep, isolated_call_log):
        ok = ProviderResponse(text="ok", input_tokens=1000, output_tokens=200)
        provider = MagicMock()
        provider.name = "anthropic"
        provider.complete.side_effect = [TransientProviderError("429"), ok]

        _call_with_retry(provider, "s", "u", "claude-sonnet-4-6", 4096, 0.0)

        records = _read_log(isolated_call_log)
        # Cross-check: log claims two attempts — the mock must confirm exactly two calls
        assert provider.complete.call_count == 2
        assert len(records) == 2
        assert records[0]["outcome"] == "error:transient"
        assert records[1]["outcome"] == "success"

        for record in records:
            for field in ("timestamp", "provider", "model", "input_tokens",
                          "output_tokens", "cost_usd", "latency_ms", "outcome"):
                assert field in record, f"missing field: {field}"
            assert record["provider"] == "anthropic"
            assert record["model"] == "claude-sonnet-4-6"

        # Success record answers "how much did it cost and who answered"
        assert records[1]["input_tokens"] == 1000
        assert records[1]["output_tokens"] == 200
        assert records[1]["cost_usd"] == pytest.approx(0.006)  # 1000*$3 + 200*$15 per MTok
        assert records[1]["latency_ms"] >= 0

    def test_fatal_run_logs_error_record(self, isolated_call_log):
        provider = MagicMock()
        provider.name = "anthropic"
        provider.complete.side_effect = FatalProviderError("bad key")

        with pytest.raises(FatalProviderError):
            _call_with_retry(provider, "s", "u", "claude-sonnet-4-6", 4096, 0.0)

        records = _read_log(isolated_call_log)
        assert provider.complete.call_count == 1
        assert len(records) == 1
        assert records[0]["outcome"] == "error:fatal"
        assert records[0]["error"] == "bad key"
        assert records[0]["cost_usd"] is None


class TestComplete:
    @patch("vetter.router.anthropic.Anthropic")
    def test_success_returns_text_and_logs(self, mock_class, isolated_call_log):
        mock_client = MagicMock()
        mock_class.return_value = mock_client
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text="the review")]
        mock_message.usage.input_tokens = 50
        mock_message.usage.output_tokens = 10
        mock_client.messages.create.return_value = mock_message

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            text = complete("system", "user", model="sonnet")

        assert text == "the review"
        # Alias resolved to the provider-specific model id
        assert mock_client.messages.create.call_args.kwargs["model"] == "claude-sonnet-4-6"

        records = _read_log(isolated_call_log)
        assert mock_client.messages.create.call_count == 1
        assert len(records) == 1
        assert records[0]["outcome"] == "success"

    def test_missing_api_key_raises_click_exception(self):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(click.ClickException, match="ANTHROPIC_API_KEY"):
                complete("system", "user")

    @patch("vetter.router.time.sleep")
    @patch("vetter.router.anthropic.Anthropic")
    def test_exhausted_transient_raises_click_exception(self, mock_class, mock_sleep):
        mock_class.return_value = _make_client_raising(
            _status_error(anthropic.RateLimitError, 429)
        )
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with pytest.raises(click.ClickException, match="unavailable after"):
                complete("system", "user")
        assert mock_class.return_value.messages.create.call_count == MAX_ATTEMPTS
