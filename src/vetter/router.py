"""Provider routing layer: client instantiation, retries, and per-call logging.

reviewer.py delegates all model I/O here and never touches provider SDKs.
Errors from provider SDKs are translated into two router-level categories:

- TransientProviderError: retried with exponential backoff (rate limits,
  server errors, timeouts, connection failures).
- FatalProviderError: fails fast, no retry (bad credentials, bad request,
  unknown model) — retrying cannot fix these.

Every call attempt (success or failure) is appended as one JSON line to the
call log. The log is append-only and accumulates across runs (no rotation;
each record carries a timestamp). Default location: ~/.vetter/calls.jsonl,
overridable with the VETTER_LOG_DIR environment variable (a directory; the
file inside is always named calls.jsonl).
"""

import json
import os
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import anthropic
import click
import openai


class TransientProviderError(Exception):
    """Retryable provider failure: rate limit, server error, timeout, connection."""


class FatalProviderError(Exception):
    """Non-retryable provider failure: credentials, permissions, bad request."""


# Model alias → per-provider model id, matched by tier (balanced/top/cheap).
MODEL_MAP = {
    "sonnet": {"anthropic": "claude-sonnet-4-6", "openai": "gpt-5.6-terra"},
    "opus": {"anthropic": "claude-opus-4-6", "openai": "gpt-5.6-sol"},
    "haiku": {"anthropic": "claude-haiku-4-5-20251001", "openai": "gpt-5.6-luna"},
}

# When the user passes a raw model id instead of an alias, it goes to the
# primary provider unchanged; a fallback provider can't use it and gets its
# default model instead.
DEFAULT_MODELS = {"openai": "gpt-5.6-terra"}

# USD per million tokens (input, output). Checked against provider pricing
# docs on 2026-07-12. Unknown models log cost_usd as null rather than a guess.
PRICING = {
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "gpt-5.6-terra": (2.50, 15.00),
    "gpt-5.6-sol": (5.00, 30.00),
    "gpt-5.6-luna": (1.00, 6.00),
}

MAX_ATTEMPTS = 3
BASE_DELAY_S = 2.0
MAX_TOOL_TURNS = 5  # tool-executing rounds before the exchange is aborted


@dataclass
class ProviderResponse:
    text: str
    input_tokens: int
    output_tokens: int


@dataclass
class ToolSpec:
    """Provider-neutral tool declaration; each provider maps it to its wire format."""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})


# A tool handler executes one tool call: (tool_name, tool_input) -> result string.
ToolHandler = Callable[[str, dict[str, Any]], str]


@dataclass
class CallRecord:
    """One log line per call attempt — the receipt (who, how much, how long).

    Failed attempts carry null tokens/cost: the SDK returns no usage on errors,
    and an honest null beats an estimate.
    """

    timestamp: str
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    latency_ms: int
    outcome: str  # "success" | "error:transient" | "error:fatal"
    error: str | None = None


class Provider(Protocol):
    name: str

    def complete(
        self, system: str, user_content: str, model_id: str, max_tokens: int, temperature: float
    ) -> ProviderResponse: ...


class AnthropicProvider:
    name = "anthropic"

    def __init__(self) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise FatalProviderError("ANTHROPIC_API_KEY environment variable is not set.")
        self._client = anthropic.Anthropic(api_key=api_key)

    def create_message(
        self,
        system: str,
        messages: list[dict],
        model_id: str,
        max_tokens: int,
        temperature: float,
        tools: list[ToolSpec] | None = None,
    ):
        """Raw single API turn: full message history in, raw SDK message out."""
        kwargs: dict[str, Any] = dict(
            model=model_id,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=messages,
        )
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ]
        try:
            return self._client.messages.create(**kwargs)
        except anthropic.AuthenticationError:
            raise FatalProviderError("anthropic: invalid ANTHROPIC_API_KEY. Please check your API key.")
        except anthropic.PermissionDeniedError as e:
            raise FatalProviderError(f"anthropic: permission denied: {e}")
        except anthropic.NotFoundError as e:
            raise FatalProviderError(f"anthropic: model or endpoint not found: {e}")
        except anthropic.BadRequestError as e:
            raise FatalProviderError(f"anthropic: bad request: {e}")
        except (anthropic.RateLimitError, anthropic.InternalServerError) as e:
            raise TransientProviderError(f"anthropic: {e}")
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                raise TransientProviderError(f"anthropic: {e}")
            raise FatalProviderError(f"anthropic: {e}")
        except anthropic.APIConnectionError as e:  # includes APITimeoutError
            raise TransientProviderError(f"anthropic: {e}")

    def complete(
        self, system: str, user_content: str, model_id: str, max_tokens: int, temperature: float
    ) -> ProviderResponse:
        message = self.create_message(
            system, [{"role": "user", "content": user_content}], model_id, max_tokens, temperature
        )
        return ProviderResponse(
            text=message.content[0].text,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )


class OpenAIProvider:
    name = "openai"

    def __init__(self) -> None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise FatalProviderError("OPENAI_API_KEY environment variable is not set.")
        self._client = openai.OpenAI(api_key=api_key)

    def complete(
        self, system: str, user_content: str, model_id: str, max_tokens: int, temperature: float
    ) -> ProviderResponse:
        # temperature is intentionally not sent: OpenAI reasoning models (gpt-5.x)
        # reject non-default sampling params with a 400, which would turn a
        # working fallback into a fatal error.
        try:
            completion = self._client.chat.completions.create(
                model=model_id,
                max_completion_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_content},
                ],
            )
        except openai.AuthenticationError:
            raise FatalProviderError("openai: invalid OPENAI_API_KEY. Please check your API key.")
        except openai.PermissionDeniedError as e:
            raise FatalProviderError(f"openai: permission denied: {e}")
        except openai.NotFoundError as e:
            raise FatalProviderError(f"openai: model or endpoint not found: {e}")
        except openai.BadRequestError as e:
            raise FatalProviderError(f"openai: bad request: {e}")
        except (openai.RateLimitError, openai.InternalServerError) as e:
            raise TransientProviderError(f"openai: {e}")
        except openai.APIStatusError as e:
            if e.status_code >= 500:
                raise TransientProviderError(f"openai: {e}")
            raise FatalProviderError(f"openai: {e}")
        except openai.APIConnectionError as e:  # includes APITimeoutError
            raise TransientProviderError(f"openai: {e}")
        return ProviderResponse(
            text=completion.choices[0].message.content or "",
            input_tokens=completion.usage.prompt_tokens,
            output_tokens=completion.usage.completion_tokens,
        )


def _resolve_model(model: str, provider_name: str) -> str:
    if model in MODEL_MAP:
        return MODEL_MAP[model][provider_name]
    return DEFAULT_MODELS.get(provider_name, model)


def _log_path() -> Path:
    log_dir = os.environ.get("VETTER_LOG_DIR")
    base = Path(log_dir) if log_dir else Path.home() / ".vetter"
    return base / "calls.jsonl"


def _append_record(record: CallRecord) -> None:
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(record)) + "\n")


def _cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float | None:
    rates = PRICING.get(model_id)
    if rates is None:
        return None
    input_rate, output_rate = rates
    return round((input_tokens * input_rate + output_tokens * output_rate) / 1_000_000, 6)


def _record(
    provider: Provider,
    model_id: str,
    started: float,
    outcome: str,
    response: ProviderResponse | None = None,
    error: Exception | None = None,
) -> None:
    _append_record(
        CallRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            provider=provider.name,
            model=model_id,
            input_tokens=response.input_tokens if response else None,
            output_tokens=response.output_tokens if response else None,
            cost_usd=_cost_usd(model_id, response.input_tokens, response.output_tokens)
            if response
            else None,
            latency_ms=int((time.monotonic() - started) * 1000),
            outcome=outcome,
            error=str(error) if error else None,
        )
    )


def _call_with_retry(
    provider: Provider,
    system: str,
    user_content: str,
    model_id: str,
    max_tokens: int,
    temperature: float,
    success_outcome: str = "success",
) -> ProviderResponse:
    for attempt in range(MAX_ATTEMPTS):
        started = time.monotonic()
        try:
            response = provider.complete(system, user_content, model_id, max_tokens, temperature)
        except TransientProviderError as e:
            _record(provider, model_id, started, "error:transient", error=e)
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(BASE_DELAY_S * (2**attempt) + random.uniform(0, 1))
                continue
            raise
        except FatalProviderError as e:
            _record(provider, model_id, started, "error:fatal", error=e)
            raise
        _record(provider, model_id, started, success_outcome, response=response)
        return response
    raise AssertionError("unreachable")


def _create_message_with_retry(
    provider: AnthropicProvider,
    system: str,
    messages: list[dict],
    model_id: str,
    max_tokens: int,
    temperature: float,
    tools: list[ToolSpec],
):
    """One tool-exchange turn with the same retry/backoff and per-call logging."""
    for attempt in range(MAX_ATTEMPTS):
        started = time.monotonic()
        try:
            message = provider.create_message(system, messages, model_id, max_tokens, temperature, tools)
        except TransientProviderError as e:
            _record(provider, model_id, started, "error:transient", error=e)
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(BASE_DELAY_S * (2**attempt) + random.uniform(0, 1))
                continue
            raise
        except FatalProviderError as e:
            _record(provider, model_id, started, "error:fatal", error=e)
            raise
        _record(
            provider,
            model_id,
            started,
            "success",
            response=ProviderResponse(
                text="",
                input_tokens=message.usage.input_tokens,
                output_tokens=message.usage.output_tokens,
            ),
        )
        return message
    raise AssertionError("unreachable")


def _run_tool_exchange(
    provider: AnthropicProvider,
    system: str,
    user_content: str,
    model_id: str,
    max_tokens: int,
    temperature: float,
    tools: list[ToolSpec],
    tool_handler: ToolHandler,
) -> ProviderResponse:
    """Drive the tool_use loop until the model stops calling tools.

    Anthropic-only by design: the fallback provider runs tool-less (declared
    degradation — resilience path delivers a review, not tool parity).
    """
    messages: list[dict] = [{"role": "user", "content": user_content}]
    last = None
    for _turn in range(MAX_TOOL_TURNS + 1):
        message = _create_message_with_retry(
            provider, system, messages, model_id, max_tokens, temperature, tools
        )
        last = message
        if message.stop_reason != "tool_use":
            text = next((b.text for b in message.content if b.type == "text"), "")
            return ProviderResponse(
                text=text,
                input_tokens=message.usage.input_tokens,
                output_tokens=message.usage.output_tokens,
            )
        tool_uses = [b for b in message.content if b.type == "tool_use"]
        messages.append({"role": "assistant", "content": message.content})
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": tool_handler(block.name, block.input),
                }
                for block in tool_uses
            ],
        })
    raise FatalProviderError(
        f"anthropic: tool exchange still requesting tools after {MAX_TOOL_TURNS} rounds "
        f"(last stop_reason: {last.stop_reason})"
    )


def complete(
    system: str,
    user_content: str,
    model: str = "sonnet",
    max_tokens: int = 4096,
    temperature: float = 0.0,
) -> str:
    """Route a completion request through the resilience ladder and return the text.

    Ladder: primary (anthropic) with retry+backoff on transient errors; if it
    stays down, fall back to openai; fatal errors fail fast at any rung. When
    the fallback answers, a notice goes to stderr so "who answered" never
    requires opening the call log. Callers stay ignorant of which provider SDK
    answered; failures surface as click.ClickException.
    """
    try:
        primary = AnthropicProvider()
    except FatalProviderError as e:
        raise click.ClickException(str(e))
    primary_model = _resolve_model(model, primary.name)

    try:
        return _call_with_retry(primary, system, user_content, primary_model, max_tokens, temperature).text
    except FatalProviderError as e:
        raise click.ClickException(str(e))
    except TransientProviderError as e:
        primary_error = e

    return _fallback_completion(
        system, user_content, model, max_tokens, temperature, primary_model, primary_error
    )


def _fallback_completion(
    system: str,
    user_content: str,
    model: str,
    max_tokens: int,
    temperature: float,
    primary_model: str,
    primary_error: TransientProviderError,
    tools_degraded: bool = False,
) -> str:
    """Climb the ladder: primary exhausted its retries on transient errors only."""
    try:
        fallback = OpenAIProvider()
    except FatalProviderError as e:
        raise click.ClickException(
            f"anthropic ({primary_model}) unavailable after {MAX_ATTEMPTS} attempts "
            f"({primary_error}) and fallback is not available: {e}"
        )
    fallback_model = _resolve_model(model, fallback.name)
    degradation = (
        " Scan tools are not supported on the fallback provider; the review proceeds without scan tools."
        if tools_degraded
        else ""
    )
    click.echo(
        f"Warning: anthropic ({primary_model}) unavailable after {MAX_ATTEMPTS} attempts; "
        f"falling back to openai ({fallback_model}).{degradation}",
        err=True,
    )
    try:
        response = _call_with_retry(
            fallback, system, user_content, fallback_model, max_tokens, temperature,
            success_outcome="fallback_success",
        )
    except (TransientProviderError, FatalProviderError) as fallback_error:
        raise click.ClickException(
            "Both providers failed. "
            f"anthropic ({primary_model}): {primary_error} | "
            f"openai ({fallback_model}): {fallback_error}"
        )
    click.echo(
        f"Note: this review was produced by openai ({fallback_model}), "
        f"not the requested anthropic model ({primary_model}).",
        err=True,
    )
    return response.text


def complete_with_tools(
    system: str,
    user_content: str,
    tools: list[ToolSpec],
    tool_handler: ToolHandler,
    model: str = "sonnet",
    max_tokens: int = 4096,
    temperature: float = 0.0,
) -> str:
    """Route a completion that may invoke tools mid-exchange.

    Same resilience ladder as complete(), with one declared degradation: the
    fallback provider does not receive the tools — a tool-less review beats no
    review on the resilience path, and the post-review gate still runs.
    """
    try:
        primary = AnthropicProvider()
    except FatalProviderError as e:
        raise click.ClickException(str(e))
    primary_model = _resolve_model(model, primary.name)

    try:
        return _run_tool_exchange(
            primary, system, user_content, primary_model, max_tokens, temperature, tools, tool_handler
        ).text
    except FatalProviderError as e:
        raise click.ClickException(str(e))
    except TransientProviderError as e:
        primary_error = e

    return _fallback_completion(
        system, user_content, model, max_tokens, temperature, primary_model, primary_error,
        tools_degraded=True,
    )
