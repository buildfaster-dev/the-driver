import json
from unittest.mock import MagicMock, patch

import anthropic
import click
import httpx
import openai
import pytest

from vetter.models import FileInfo, RepoData, ScanResult
from vetter.reviewer import review_repo
from vetter.router import (
    MAX_ATTEMPTS,
    AnthropicProvider,
    FatalProviderError,
    OpenAIProvider,
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
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "the review"
        mock_message.stop_reason = "end_turn"
        mock_message.content = [text_block]
        mock_message.usage.input_tokens = 50
        mock_message.usage.output_tokens = 10
        mock_message.usage.cache_read_input_tokens = 0
        mock_message.usage.cache_creation_input_tokens = 0
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
    def test_exhausted_transient_without_fallback_key_raises(self, mock_class, mock_sleep, monkeypatch):
        mock_class.return_value = _make_client_raising(
            _status_error(anthropic.RateLimitError, 429)
        )
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with pytest.raises(click.ClickException, match="fallback is not available"):
                complete("system", "user")
        assert mock_class.return_value.messages.create.call_count == MAX_ATTEMPTS


def _openai_status_error(cls, status_code):
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(status_code, request=request)
    return cls("boom", response=response, body=None)


def _openai_client_returning(text, prompt_tokens=80, completion_tokens=20):
    client = MagicMock()
    completion = MagicMock()
    choice = MagicMock()
    choice.message.content = text
    completion.choices = [choice]
    completion.usage.prompt_tokens = prompt_tokens
    completion.usage.completion_tokens = completion_tokens
    client.chat.completions.create.return_value = completion
    return client


BOTH_KEYS = {"ANTHROPIC_API_KEY": "test-key-a", "OPENAI_API_KEY": "test-key-o"}


class TestOpenAIProviderErrorTranslation:
    """Inject real openai SDK exception classes; assert the router-level category."""

    def _provider_with(self, exc):
        client = MagicMock()
        client.chat.completions.create.side_effect = exc
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            with patch("vetter.router.openai.OpenAI") as mock_class:
                mock_class.return_value = client
                provider = OpenAIProvider()
        return provider

    @pytest.mark.parametrize("exc_class,status", [
        (openai.RateLimitError, 429),
        (openai.InternalServerError, 500),
        (openai.InternalServerError, 503),
    ])
    def test_transient_status_errors(self, exc_class, status):
        provider = self._provider_with(_openai_status_error(exc_class, status))
        with pytest.raises(TransientProviderError):
            provider.complete(**CALL_ARGS)

    def test_timeout_is_transient(self):
        request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        provider = self._provider_with(openai.APITimeoutError(request=request))
        with pytest.raises(TransientProviderError):
            provider.complete(**CALL_ARGS)

    @pytest.mark.parametrize("exc_class,status", [
        (openai.AuthenticationError, 401),
        (openai.PermissionDeniedError, 403),
        (openai.NotFoundError, 404),
        (openai.BadRequestError, 400),
    ])
    def test_fatal_status_errors(self, exc_class, status):
        provider = self._provider_with(_openai_status_error(exc_class, status))
        with pytest.raises(FatalProviderError):
            provider.complete(**CALL_ARGS)

    def test_missing_api_key_is_fatal(self):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(FatalProviderError, match="OPENAI_API_KEY"):
                OpenAIProvider()


class TestOpenAIProviderRequestShape:
    @patch("vetter.router.openai.OpenAI")
    def test_normalizes_response_and_request(self, mock_class):
        mock_class.return_value = _openai_client_returning("hello", 80, 20)
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            provider = OpenAIProvider()
        response = provider.complete("sys", "usr", "gpt-5.6-terra", 4096, 0.0)

        assert response == ProviderResponse(text="hello", input_tokens=80, output_tokens=20)

        kwargs = mock_class.return_value.chat.completions.create.call_args.kwargs
        assert kwargs["model"] == "gpt-5.6-terra"
        assert kwargs["max_completion_tokens"] == 4096
        assert "temperature" not in kwargs  # reasoning models 400 on non-default sampling
        assert kwargs["messages"][0] == {"role": "system", "content": "sys"}
        assert kwargs["messages"][1] == {"role": "user", "content": "usr"}


class TestFallbackLadder:
    """Eval 1: effects on both SDK mocks, never the router's own announcements."""

    @patch("vetter.router.time.sleep")
    @patch("vetter.router.openai.OpenAI")
    @patch("vetter.router.anthropic.Anthropic")
    def test_transient_primary_falls_back_to_openai(
        self, mock_anthropic, mock_openai, mock_sleep, capsys
    ):
        mock_anthropic.return_value = _make_client_raising(
            _status_error(anthropic.RateLimitError, 429)
        )
        mock_openai.return_value = _openai_client_returning("openai says hi")

        with patch.dict("os.environ", BOTH_KEYS):
            text = complete("system", "user", model="sonnet")

        assert text == "openai says hi"
        # Primary confirmed called and failed; secondary confirmed called and answered
        assert mock_anthropic.return_value.messages.create.call_count == MAX_ATTEMPTS
        assert mock_openai.return_value.chat.completions.create.call_count == 1

        # Ajuste 4: who answered is visible on stderr, not only in the log
        err = capsys.readouterr().err
        assert "falling back to openai (gpt-5.6-terra)" in err
        assert "produced by openai (gpt-5.6-terra)" in err

    @patch("vetter.router.openai.OpenAI")
    @patch("vetter.router.anthropic.Anthropic")
    def test_auth_error_fails_fast_without_fallback(self, mock_anthropic, mock_openai):
        mock_anthropic.return_value = _make_client_raising(
            _status_error(anthropic.AuthenticationError, 401)
        )
        with patch.dict("os.environ", BOTH_KEYS):
            with pytest.raises(click.ClickException, match="ANTHROPIC_API_KEY"):
                complete("system", "user")

        assert mock_anthropic.return_value.messages.create.call_count == 1
        mock_openai.assert_not_called()

    @patch("vetter.router.time.sleep")
    @patch("vetter.router.openai.OpenAI")
    @patch("vetter.router.anthropic.Anthropic")
    def test_both_providers_down(self, mock_anthropic, mock_openai, mock_sleep):
        mock_anthropic.return_value = _make_client_raising(
            _status_error(anthropic.InternalServerError, 529)
        )
        openai_client = MagicMock()
        openai_client.chat.completions.create.side_effect = _openai_status_error(
            openai.InternalServerError, 500
        )
        mock_openai.return_value = openai_client

        with patch.dict("os.environ", BOTH_KEYS):
            with pytest.raises(click.ClickException, match="Both providers failed"):
                complete("system", "user")

        assert mock_anthropic.return_value.messages.create.call_count == MAX_ATTEMPTS
        assert openai_client.chat.completions.create.call_count == MAX_ATTEMPTS

    @patch("vetter.router.time.sleep")
    @patch("vetter.router.openai.OpenAI")
    @patch("vetter.router.anthropic.Anthropic")
    def test_fatal_on_fallback_fails_fast(self, mock_anthropic, mock_openai, mock_sleep):
        mock_anthropic.return_value = _make_client_raising(
            _status_error(anthropic.RateLimitError, 429)
        )
        openai_client = MagicMock()
        openai_client.chat.completions.create.side_effect = _openai_status_error(
            openai.AuthenticationError, 401
        )
        mock_openai.return_value = openai_client

        with patch.dict("os.environ", BOTH_KEYS):
            with pytest.raises(click.ClickException, match="Both providers failed"):
                complete("system", "user")

        # Fatal on the fallback rung: one call, no retries there
        assert openai_client.chat.completions.create.call_count == 1


class TestCallLogFallbackCross:
    """Ajuste 2: the JSONL testimony must match the mocks' independent evidence."""

    @patch("vetter.router.time.sleep")
    @patch("vetter.router.openai.OpenAI")
    @patch("vetter.router.anthropic.Anthropic")
    def test_log_matches_mock_effects(
        self, mock_anthropic, mock_openai, mock_sleep, isolated_call_log
    ):
        mock_anthropic.return_value = _make_client_raising(
            _status_error(anthropic.RateLimitError, 429)
        )
        mock_openai.return_value = _openai_client_returning("ok", 80, 20)

        with patch.dict("os.environ", BOTH_KEYS):
            complete("system", "user", model="sonnet")

        records = _read_log(isolated_call_log)
        anthropic_records = [r for r in records if r["provider"] == "anthropic"]
        openai_records = [r for r in records if r["provider"] == "openai"]

        # Log claims N failed anthropic attempts → the mock must confirm exactly N calls
        assert len(anthropic_records) == mock_anthropic.return_value.messages.create.call_count == MAX_ATTEMPTS
        assert all(r["outcome"] == "error:transient" for r in anthropic_records)
        assert all(r["model"] == "claude-sonnet-4-6" for r in anthropic_records)

        # Log claims one openai answer → the mock must confirm exactly one call
        assert len(openai_records) == mock_openai.return_value.chat.completions.create.call_count == 1
        assert openai_records[0]["outcome"] == "fallback_success"
        assert openai_records[0]["model"] == "gpt-5.6-terra"
        assert openai_records[0]["input_tokens"] == 80
        assert openai_records[0]["output_tokens"] == 20
        # 80*$2.50 + 20*$15.00 per MTok
        assert openai_records[0]["cost_usd"] == pytest.approx(0.0005)


REVIEW_JSON = json.dumps({
    "architecture_awareness": {
        "score": 4, "justification": "Solid layout.", "evidence": ["app.py:1 — layering"],
    },
    "code_refinement": {
        "score": 3, "justification": "Readable.", "evidence": ["app.py:5 — idioms"],
    },
    "edge_case_coverage": {
        "score": 2, "justification": "Few tests.", "evidence": ["tests/ — sparse"],
    },
    "overall_summary": "Acceptable submission.",
})


class TestPipelineFallback:
    """Eval 1 end-to-end: primary down (simulated) → fallback yields a valid ReviewResult."""

    @patch("vetter.router.time.sleep")
    @patch("vetter.router.openai.OpenAI")
    @patch("vetter.router.anthropic.Anthropic")
    def test_review_repo_survives_primary_outage(self, mock_anthropic, mock_openai, mock_sleep):
        mock_anthropic.return_value = _make_client_raising(
            _status_error(anthropic.RateLimitError, 429)
        )
        mock_openai.return_value = _openai_client_returning(REVIEW_JSON)
        repo = RepoData(
            path="/fake/repo",
            files=[FileInfo("app.py", "print('hi')", "Python", 11, False)],
            commits=[], languages={"Python": 1}, total_files=1, total_lines=1,
        )
        scan = ScanResult(
            test_ratio=0.0, has_linter_config=False, linter_configs_found=[],
            commit_count=1, commit_quality="poor", commit_messages=["init"],
            dependencies=[], error_handling="minimal", security_flags=[],
            languages={"Python": 1},
        )

        with patch.dict("os.environ", BOTH_KEYS):
            result = review_repo(repo, scan, model="sonnet")

        assert result.pillar("architecture_awareness").score == 4
        assert result.pillar("edge_case_coverage").score == 2
        assert mock_anthropic.return_value.messages.create.call_count == MAX_ATTEMPTS
        assert mock_openai.return_value.chat.completions.create.call_count == 1


from vetter.router import MAX_TOOL_TURNS, ToolSpec, complete_with_tools  # noqa: E402


def _tool_use_message(name="get_scan_summary", tool_id="tu_1", tool_input=None):
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_id
    block.name = name
    block.input = tool_input or {}
    msg = MagicMock()
    msg.stop_reason = "tool_use"
    msg.content = [block]
    msg.usage.input_tokens = 100
    msg.usage.output_tokens = 20
    msg.usage.cache_read_input_tokens = 0
    msg.usage.cache_creation_input_tokens = 0
    return msg


def _text_message(text):
    block = MagicMock()
    block.type = "text"
    block.text = text
    msg = MagicMock()
    msg.stop_reason = "end_turn"
    msg.content = [block]
    msg.usage.input_tokens = 100
    msg.usage.output_tokens = 30
    msg.usage.cache_read_input_tokens = 0
    msg.usage.cache_creation_input_tokens = 0
    return msg


SCAN_TOOL = ToolSpec(name="get_scan_summary", description="Real scan findings.")


class TestToolExchange:
    """Eval 1: tool invocations verified in the actual message exchange."""

    @patch("vetter.router.anthropic.Anthropic")
    def test_tool_call_round_trip_feeds_real_result_back(self, mock_class):
        mock_client = MagicMock()
        mock_class.return_value = mock_client
        mock_client.messages.create.side_effect = [
            _tool_use_message(tool_id="tu_42", tool_input={"section": "all"}),
            _text_message("final review"),
        ]
        handler = MagicMock(return_value='{"error_handling": "minimal"}')

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            text = complete_with_tools("sys", "usr", [SCAN_TOOL], handler, model="sonnet")

        assert text == "final review"
        assert mock_client.messages.create.call_count == 2
        handler.assert_called_once_with("get_scan_summary", {"section": "all"})

        # Tools are OFFERED on every turn, in Anthropic wire format
        for call in mock_client.messages.create.call_args_list:
            assert call.kwargs["tools"] == [{
                "name": "get_scan_summary",
                "description": "Real scan findings.",
                "input_schema": {"type": "object", "properties": {}},
            }]

        # The second turn carries the handler's real output as the tool_result
        second_messages = mock_client.messages.create.call_args_list[1].kwargs["messages"]
        assert second_messages[1]["role"] == "assistant"
        tool_results = second_messages[2]
        assert tool_results["role"] == "user"
        assert tool_results["content"] == [{
            "type": "tool_result",
            "tool_use_id": "tu_42",
            "content": '{"error_handling": "minimal"}',
        }]

    @patch("vetter.router.anthropic.Anthropic")
    def test_no_tool_use_returns_text_without_invoking_handler(self, mock_class):
        mock_client = MagicMock()
        mock_class.return_value = mock_client
        mock_client.messages.create.return_value = _text_message("straight answer")
        handler = MagicMock()

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            text = complete_with_tools("sys", "usr", [SCAN_TOOL], handler)

        assert text == "straight answer"
        assert mock_client.messages.create.call_count == 1
        handler.assert_not_called()

    @patch("vetter.router.anthropic.Anthropic")
    def test_endless_tool_requests_abort_with_clear_error(self, mock_class):
        mock_client = MagicMock()
        mock_class.return_value = mock_client
        mock_client.messages.create.return_value = _tool_use_message()
        handler = MagicMock(return_value="{}")

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with pytest.raises(click.ClickException, match="tool exchange"):
                complete_with_tools("sys", "usr", [SCAN_TOOL], handler)

        assert mock_client.messages.create.call_count == MAX_TOOL_TURNS + 1

    @patch("vetter.router.time.sleep")
    @patch("vetter.router.openai.OpenAI")
    @patch("vetter.router.anthropic.Anthropic")
    def test_fallback_degrades_to_toolless_and_declares_it(
        self, mock_anthropic, mock_openai, mock_sleep, capsys
    ):
        mock_anthropic.return_value = _make_client_raising(
            _status_error(anthropic.RateLimitError, 429)
        )
        mock_openai.return_value = _openai_client_returning("toolless review")
        handler = MagicMock()

        with patch.dict("os.environ", BOTH_KEYS):
            text = complete_with_tools("sys", "usr", [SCAN_TOOL], handler, model="sonnet")

        assert text == "toolless review"
        handler.assert_not_called()
        openai_kwargs = mock_openai.return_value.chat.completions.create.call_args.kwargs
        assert "tools" not in openai_kwargs  # degradation is real, not cosmetic
        assert "without scan tools" in capsys.readouterr().err  # and declared

    @patch("vetter.router.anthropic.Anthropic")
    def test_each_turn_leaves_a_call_record(self, mock_class, isolated_call_log):
        mock_client = MagicMock()
        mock_class.return_value = mock_client
        mock_client.messages.create.side_effect = [
            _tool_use_message(),
            _text_message("done"),
        ]

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            complete_with_tools("sys", "usr", [SCAN_TOOL], MagicMock(return_value="{}"))

        records = _read_log(isolated_call_log)
        # Log testimony crossed against the mock's independent evidence
        assert len(records) == mock_client.messages.create.call_count == 2
        assert all(r["outcome"] == "success" for r in records)
        assert all(r["provider"] == "anthropic" for r in records)


from vetter.router import (  # noqa: E402
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    MIN_CACHEABLE_TOKENS,
    _cost_usd,
)

LARGE_CONTENT = "x" * (MIN_CACHEABLE_TOKENS * 3 + 100)  # above the marking threshold
LARGE_SYSTEM = "s" * (MIN_CACHEABLE_TOKENS * 3 + 100)


class TestPromptCacheMarking:
    """Effects: the cache_control kwarg travels (or not) in the real SDK call."""

    def _client_with_response(self):
        client = MagicMock()
        client.messages.create.return_value = _text_message("ok")
        return client

    @patch("vetter.router.anthropic.Anthropic")
    def test_large_user_content_is_marked_without_changing_bytes(self, mock_class):
        mock_class.return_value = self._client_with_response()

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            complete("small system", LARGE_CONTENT)

        kwargs = mock_class.return_value.messages.create.call_args.kwargs
        first = kwargs["messages"][0]
        assert first["role"] == "user"
        assert first["content"] == [{
            "type": "text",
            "text": LARGE_CONTENT,  # byte-identical, only wrapped
            "cache_control": {"type": "ephemeral"},
        }]
        # small system stays a plain string
        assert kwargs["system"] == "small system"

    @patch("vetter.router.anthropic.Anthropic")
    def test_large_system_is_marked_too(self, mock_class):
        mock_class.return_value = self._client_with_response()

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            complete(LARGE_SYSTEM, "small question")

        kwargs = mock_class.return_value.messages.create.call_args.kwargs
        assert kwargs["system"] == [{
            "type": "text",
            "text": LARGE_SYSTEM,
            "cache_control": {"type": "ephemeral"},
        }]
        # small user content stays a plain string
        assert kwargs["messages"][0]["content"] == "small question"

    @patch("vetter.router.anthropic.Anthropic")
    def test_small_prompts_are_not_marked(self, mock_class):
        mock_class.return_value = self._client_with_response()

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            complete("small system", "small question")

        kwargs = mock_class.return_value.messages.create.call_args.kwargs
        assert kwargs["system"] == "small system"
        assert kwargs["messages"][0]["content"] == "small question"

    @patch("vetter.router.anthropic.Anthropic")
    def test_tool_loop_marks_first_user_message_on_every_turn(self, mock_class):
        mock_client = MagicMock()
        mock_class.return_value = mock_client
        mock_client.messages.create.side_effect = [
            _tool_use_message(tool_id="tu_1"),
            _text_message("done"),
        ]

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            complete_with_tools("sys", LARGE_CONTENT, [SCAN_TOOL], MagicMock(return_value="{}"))

        for call in mock_client.messages.create.call_args_list:
            first = call.kwargs["messages"][0]
            assert first["content"][0]["cache_control"] == {"type": "ephemeral"}
            assert first["content"][0]["text"] == LARGE_CONTENT
        # turn 2: assistant/tool_result blocks are NOT cache-marked
        second_turn = mock_client.messages.create.call_args_list[1].kwargs["messages"]
        tool_result = second_turn[2]["content"][0]
        assert "cache_control" not in tool_result

    @patch("vetter.router.time.sleep")
    @patch("vetter.router.openai.OpenAI")
    @patch("vetter.router.anthropic.Anthropic")
    def test_cache_marking_never_travels_to_openai_fallback(
        self, mock_anthropic, mock_openai, mock_sleep
    ):
        mock_anthropic.return_value = _make_client_raising(
            _status_error(anthropic.RateLimitError, 429)
        )
        mock_openai.return_value = _openai_client_returning("fallback answer")

        with patch.dict("os.environ", BOTH_KEYS):
            complete(LARGE_SYSTEM, LARGE_CONTENT)

        kwargs = mock_openai.return_value.chat.completions.create.call_args.kwargs
        for message in kwargs["messages"]:
            assert isinstance(message["content"], str)  # plain strings, no blocks
        assert "cache_control" not in json.dumps(kwargs["messages"])
        assert "system" not in kwargs  # OpenAI wire shape untouched


class TestCacheAccounting:
    def test_cost_formula_write_and_read_rates(self):
        # sonnet: $3/MTok input. 500 uncached + 150k written + 100 out at $15.
        cost = _cost_usd("claude-sonnet-4-6", 500, 100, cache_read_tokens=0, cache_write_tokens=150_000)
        expected = (500 * 3 + 150_000 * 3 * CACHE_WRITE_MULTIPLIER + 100 * 15) / 1_000_000
        assert cost == pytest.approx(expected)  # 0.565500

        cost = _cost_usd("claude-sonnet-4-6", 500, 100, cache_read_tokens=150_000, cache_write_tokens=0)
        expected = (500 * 3 + 150_000 * 3 * CACHE_READ_MULTIPLIER + 100 * 15) / 1_000_000
        assert cost == pytest.approx(expected)  # 0.048000

    def test_none_cache_fields_cost_like_before(self):
        assert _cost_usd("claude-sonnet-4-6", 1000, 200) == pytest.approx(0.006)

    @patch("vetter.router.anthropic.Anthropic")
    def test_record_carries_cache_fields_from_usage(self, mock_class, isolated_call_log):
        mock_client = MagicMock()
        mock_class.return_value = mock_client
        msg = _text_message("ok")
        msg.usage.input_tokens = 500
        msg.usage.output_tokens = 100
        msg.usage.cache_creation_input_tokens = 150_000
        msg.usage.cache_read_input_tokens = 0
        mock_client.messages.create.return_value = msg

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            complete("sys", LARGE_CONTENT, model="sonnet")

        records = _read_log(isolated_call_log)
        assert mock_client.messages.create.call_count == len(records) == 1
        record = records[0]
        # Cache write without read stays visible — that's the cold-run receipt
        assert record["cache_write_tokens"] == 150_000
        assert record["cache_read_tokens"] == 0
        assert record["cost_usd"] == pytest.approx(0.5655)

    @patch("vetter.router.time.sleep")
    @patch("vetter.router.openai.OpenAI")
    @patch("vetter.router.anthropic.Anthropic")
    def test_openai_record_has_null_cache_fields(
        self, mock_anthropic, mock_openai, mock_sleep, isolated_call_log
    ):
        mock_anthropic.return_value = _make_client_raising(
            _status_error(anthropic.RateLimitError, 429)
        )
        mock_openai.return_value = _openai_client_returning("ok")

        with patch.dict("os.environ", BOTH_KEYS):
            complete("sys", "user", model="sonnet")

        records = _read_log(isolated_call_log)
        openai_records = [r for r in records if r["provider"] == "openai"]
        assert len(openai_records) == mock_openai.return_value.chat.completions.create.call_count == 1
        # null = not measured; distinct from a measured zero
        assert openai_records[0]["cache_read_tokens"] is None
        assert openai_records[0]["cache_write_tokens"] is None


class TestPricingFreshness:
    """Eval 3 (phase 07): stale pricing warns loudly, never crashes."""

    def _reset_flag(self, monkeypatch):
        monkeypatch.setattr("vetter.router._pricing_warning_emitted", False)

    @patch("vetter.router.anthropic.Anthropic")
    def test_expired_date_warns_and_run_continues(self, mock_class, monkeypatch, capsys):
        from datetime import date, timedelta
        self._reset_flag(monkeypatch)
        monkeypatch.setattr(
            "vetter.router.PRICING_VERIFIED_ON", date.today() - timedelta(days=120)
        )
        mock_client = MagicMock()
        mock_class.return_value = mock_client
        mock_client.messages.create.return_value = _text_message("still works")

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            text = complete("sys", "user")

        assert text == "still works"  # the run continued
        err = capsys.readouterr().err
        assert "PRICING table last verified 120 days ago" in err
        assert "re-verify" in err

    @patch("vetter.router.anthropic.Anthropic")
    def test_warning_emitted_once_per_process(self, mock_class, monkeypatch, capsys):
        from datetime import date, timedelta
        self._reset_flag(monkeypatch)
        monkeypatch.setattr(
            "vetter.router.PRICING_VERIFIED_ON", date.today() - timedelta(days=120)
        )
        mock_client = MagicMock()
        mock_class.return_value = mock_client
        mock_client.messages.create.return_value = _text_message("ok")

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            complete("sys", "user")
            complete("sys", "user")

        assert capsys.readouterr().err.count("PRICING table") == 1

    @patch("vetter.router.anthropic.Anthropic")
    def test_fresh_date_stays_silent(self, mock_class, monkeypatch, capsys):
        from datetime import date
        self._reset_flag(monkeypatch)
        monkeypatch.setattr("vetter.router.PRICING_VERIFIED_ON", date.today())
        mock_client = MagicMock()
        mock_class.return_value = mock_client
        mock_client.messages.create.return_value = _text_message("ok")

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            complete("sys", "user")

        assert "PRICING table" not in capsys.readouterr().err


from vetter.router import _first_text_block, _response_from_message  # noqa: E402


def _block(block_type, text=None):
    b = MagicMock()
    b.type = block_type
    if text is not None:
        b.text = text
    return b


def _message_with(blocks):
    msg = MagicMock()
    msg.content = blocks
    msg.usage.input_tokens = 10
    msg.usage.output_tokens = 5
    msg.usage.cache_read_input_tokens = 0
    msg.usage.cache_creation_input_tokens = 0
    return msg


class TestBlockValidation:
    """Eval 2 (phase 08): correct parse or a clear typed error, never IndexError."""

    def test_thinking_then_text_picks_the_text(self):
        msg = _message_with([_block("thinking"), _block("text", "the real answer")])
        assert _response_from_message(msg).text == "the real answer"

    def test_text_after_tool_use_is_found(self):
        msg = _message_with([_block("tool_use"), _block("text", "answer")])
        assert _first_text_block(msg) == "answer"

    def test_only_tool_use_raises_typed_error(self):
        msg = _message_with([_block("tool_use")])
        with pytest.raises(FatalProviderError, match="no text block"):
            _response_from_message(msg)

    def test_only_thinking_raises_typed_error(self):
        msg = _message_with([_block("thinking")])
        with pytest.raises(FatalProviderError, match="block types.*thinking"):
            _first_text_block(msg)

    def test_empty_content_raises_typed_error(self):
        with pytest.raises(FatalProviderError, match="no text block"):
            _first_text_block(_message_with([]))

    @patch("vetter.router.anthropic.Anthropic")
    def test_no_text_block_surfaces_as_click_exception(self, mock_class):
        mock_client = MagicMock()
        mock_class.return_value = mock_client
        mock_client.messages.create.return_value = _message_with([_block("tool_use")])

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with pytest.raises(click.ClickException, match="no text block"):
                complete("sys", "user")


from vetter.router import (  # noqa: E402
    RUN_COST_BUDGET_USD,
    BillingProviderError,
    RunLimitExceeded,
    _is_billing_error,
)


class TestBillingRouting:
    """Decision: billing-400 escalates to the fallback; 401 stays fatal."""

    def _billing_error(self, cls, status):
        err = _status_error(cls, status)
        err.type = "billing_error"
        return err

    @patch("vetter.router.time.sleep")
    @patch("vetter.router.openai.OpenAI")
    @patch("vetter.router.anthropic.Anthropic")
    def test_billing_400_falls_back_without_retry(self, mock_anthropic, mock_openai, mock_sleep, capsys):
        mock_anthropic.return_value = _make_client_raising(
            self._billing_error(anthropic.BadRequestError, 400)
        )
        mock_openai.return_value = _openai_client_returning("fallback saved it")

        with patch.dict("os.environ", BOTH_KEYS):
            text = complete("sys", "user", model="sonnet")

        assert text == "fallback saved it"
        # No same-provider retry on billing: exactly one primary attempt
        assert mock_anthropic.return_value.messages.create.call_count == 1
        assert mock_openai.return_value.chat.completions.create.call_count == 1
        assert "billing failed" in capsys.readouterr().err

    @patch("vetter.router.openai.OpenAI")
    @patch("vetter.router.anthropic.Anthropic")
    def test_auth_401_stays_fatal_no_fallback(self, mock_anthropic, mock_openai):
        mock_anthropic.return_value = _make_client_raising(
            _status_error(anthropic.AuthenticationError, 401)
        )
        with patch.dict("os.environ", BOTH_KEYS):
            with pytest.raises(click.ClickException, match="ANTHROPIC_API_KEY"):
                complete("sys", "user")
        mock_openai.assert_not_called()

    def test_billing_logged_distinctly(self, monkeypatch, isolated_call_log):
        # Billing failure gets its own outcome in the receipt.
        with patch("vetter.router.openai.OpenAI") as mock_openai, \
             patch("vetter.router.anthropic.Anthropic") as mock_anthropic:
            mock_anthropic.return_value = _make_client_raising(
                self._billing_error(anthropic.BadRequestError, 400)
            )
            mock_openai.return_value = _openai_client_returning("ok")
            with patch.dict("os.environ", BOTH_KEYS):
                complete("sys", "user")
        records = _read_log(isolated_call_log)
        assert any(r["outcome"] == "error:billing" and r["provider"] == "anthropic" for r in records)

    def test_is_billing_error_detects_message_and_type(self):
        e1 = MagicMock(); e1.type = "billing_error"
        assert _is_billing_error(e1)
        e2 = Exception("Your credit balance is too low")
        assert _is_billing_error(e2)
        e3 = Exception("messages: roles must alternate")
        assert not _is_billing_error(e3)


def _cheap_tool_message():
    """A tool_use turn with tiny cost, so the budget isn't hit by cost."""
    msg = _tool_use_message()
    msg.usage.input_tokens = 10
    msg.usage.output_tokens = 5
    return msg


class TestRunCaps:
    """Eval 3 (phase 08): a runaway run is cut cleanly with a declared reason."""

    @patch("vetter.router.anthropic.Anthropic")
    def test_cost_budget_cuts_runaway_tool_loop(self, mock_class):
        mock_client = MagicMock()
        mock_class.return_value = mock_client
        # Every turn requests tools and costs a lot → budget exceeded fast.
        expensive = _tool_use_message()
        expensive.usage.input_tokens = 200_000  # ~$0.60/turn on sonnet
        expensive.usage.output_tokens = 100
        mock_client.messages.create.return_value = expensive

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with pytest.raises(RunLimitExceeded, match="cost budget"):
                complete_with_tools("sys", "u" * 4000, [SCAN_TOOL], MagicMock(return_value="{}"))

        # Cut before burning all 5 turns (5 * $0.60 = $3.0 budget)
        assert mock_client.messages.create.call_count <= 6

    @patch("vetter.router.time.monotonic")
    @patch("vetter.router.anthropic.Anthropic")
    def test_deadline_cuts_stuck_run(self, mock_class, mock_monotonic):
        mock_client = MagicMock()
        mock_class.return_value = mock_client
        mock_client.messages.create.return_value = _cheap_tool_message()
        # First call sets started_wall=0; subsequent checks jump past the deadline.
        mock_monotonic.side_effect = [0.0] + [10_000.0] * 20

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with pytest.raises(RunLimitExceeded, match="deadline"):
                complete_with_tools("sys", "u" * 4000, [SCAN_TOOL], MagicMock(return_value="{}"))

    @patch("vetter.router.anthropic.Anthropic")
    def test_completed_run_is_never_cut(self, mock_class):
        mock_client = MagicMock()
        mock_class.return_value = mock_client
        # One tool turn, then a final answer — under budget, must return.
        mock_client.messages.create.side_effect = [_cheap_tool_message(), _text_message("done")]

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            text = complete_with_tools("sys", "u" * 4000, [SCAN_TOOL], MagicMock(return_value="{}"))

        assert text == "done"
