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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import anthropic
import click
import openai


class TransientProviderError(Exception):
    """Retryable provider failure: rate limit, server error, timeout, connection."""


class FatalProviderError(Exception):
    """Non-retryable provider failure: credentials, permissions, bad request."""


class BillingProviderError(Exception):
    """Provider billing/credit failure (e.g. 400 insufficient balance).

    Unlike auth (bad key = config, both providers likely misconfigured) or a
    malformed request (both would reject), billing is provider-specific account
    state: switching providers can succeed. So it does NOT retry on the same
    provider, but DOES escalate to the fallback.
    """


class RunLimitExceeded(Exception):
    """A single run hit its per-run cost or wall-clock budget — clean cut."""


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

# USD per million tokens (input, output). Verified on PRICING_VERIFIED_ON
# (see below). Unknown models log cost_usd as null rather than a guess.
PRICING = {
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "gpt-5.6-terra": (2.50, 15.00),
    "gpt-5.6-sol": (5.00, 30.00),
    "gpt-5.6-luna": (1.00, 6.00),
}

# Update BOTH lines when re-verifying prices against provider docs. Past
# PRICING_MAX_AGE_DAYS without re-verification, every run warns loudly on
# stderr (providers move prices on a roughly quarterly cadence; a quarter
# unverified means cost_usd may be silently rotten).
PRICING_VERIFIED_ON = date(2026, 7, 13)
PRICING_MAX_AGE_DAYS = 90
_pricing_warning_emitted = False


def _warn_if_pricing_stale() -> None:
    """Loud warning, never a crash: the run continues with suspect cost_usd."""
    global _pricing_warning_emitted
    if _pricing_warning_emitted:
        return
    age_days = (date.today() - PRICING_VERIFIED_ON).days
    if age_days > PRICING_MAX_AGE_DAYS:
        _pricing_warning_emitted = True
        click.echo(
            f"Warning: PRICING table last verified {age_days} days ago "
            f"({PRICING_VERIFIED_ON.isoformat()}, max {PRICING_MAX_AGE_DAYS}); "
            f"cost_usd in the call log may be stale — re-verify provider pricing.",
            err=True,
        )


MAX_ATTEMPTS = 3
BASE_DELAY_S = 2.0
MAX_TOOL_TURNS = 5  # tool-executing rounds before the exchange is aborted

# Per-run guardrails: a runaway tool loop or a stuck run is cut cleanly rather
# than producing a surprise bill or hanging.
RUN_COST_BUDGET_USD = 3.0
RUN_DEADLINE_S = 900.0  # 15 minutes


def _is_billing_error(e: Exception) -> bool:
    error_type = str(getattr(e, "type", "") or "")
    text = f"{error_type} {e}".lower()
    return "billing" in text or "credit balance" in text or ("insufficient" in text and "credit" in text)

# Anthropic prompt-cache rates relative to the input rate, 5m TTL (verified
# against the installed SDK's cache_control shape and provider pricing docs
# on 2026-07-13). Reads are ~10% of input price; writes carry a 25% premium.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10

# Below the provider's minimum cacheable prefix a marker is a silent no-op;
# skip marking small prompts entirely (gate resolutions, judge calls).
MIN_CACHEABLE_TOKENS = 1024
_CHARS_PER_TOKEN_ESTIMATE = 3  # conservative for code-heavy text


def _cache_eligible(text: str) -> bool:
    return len(text) >= MIN_CACHEABLE_TOKENS * _CHARS_PER_TOKEN_ESTIMATE


@dataclass
class ProviderResponse:
    text: str
    input_tokens: int
    output_tokens: int
    # None = not measured (providers without cache accounting); ints for
    # Anthropic, including 0 (a cache write without a read stays visible).
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None


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
    and an honest null beats an estimate. Cache fields are null when the
    provider doesn't report cache accounting (null = not measured, not zero).
    """

    timestamp: str
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    cost_usd: float | None
    latency_ms: int
    outcome: str  # "success" | "error:transient" | "error:fatal"
    error: str | None = None


class Provider(Protocol):
    name: str

    def complete(
        self, system: str, user_content: str, model_id: str, max_tokens: int, temperature: float
    ) -> ProviderResponse: ...


def _first_text_block(message) -> str:
    """First text block's text, skipping thinking/tool_use in any order.

    No text block at all (thinking-only, tool_use-only, empty) is a typed
    error, never a silently-wrong review — closes the content[0].text debt.
    """
    for block in message.content:
        if getattr(block, "type", None) == "text":
            return block.text
    types = [getattr(b, "type", "?") for b in message.content]
    raise FatalProviderError(
        f"anthropic: response contained no text block (block types: {types})"
    )


def _usage_only(message) -> ProviderResponse:
    """Usage + cache accounting, no text extraction — for per-turn logging.

    A tool_use turn legitimately has no text block; logging must not require
    one. Text is enforced only when a final review string is returned.
    """
    return ProviderResponse(
        text="",
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
        cache_read_tokens=message.usage.cache_read_input_tokens or 0,
        cache_write_tokens=message.usage.cache_creation_input_tokens or 0,
    )


def _response_from_message(message) -> ProviderResponse:
    """Normalize a final Anthropic message; requires a text block (typed error)."""
    response = _usage_only(message)
    response.text = _first_text_block(message)
    return response


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
        """Raw single API turn: full message history in, raw SDK message out.

        Large system prompts and the first (large) user message are marked
        with cache_control. The marking never changes prompt bytes — the text
        is wrapped in a block, not rewritten (phase-05 parity holds).
        """
        system_param: Any = system
        if _cache_eligible(system):
            system_param = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

        if (
            messages
            and messages[0]["role"] == "user"
            and isinstance(messages[0]["content"], str)
            and _cache_eligible(messages[0]["content"])
        ):
            first = {
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": messages[0]["content"],
                    "cache_control": {"type": "ephemeral"},
                }],
            }
            messages = [first, *messages[1:]]

        kwargs: dict[str, Any] = dict(
            model=model_id,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_param,
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
            if _is_billing_error(e):
                raise BillingProviderError(f"anthropic: billing/credit problem: {e}")
            raise FatalProviderError(f"anthropic: permission denied: {e}")
        except anthropic.NotFoundError as e:
            raise FatalProviderError(f"anthropic: model or endpoint not found: {e}")
        except anthropic.BadRequestError as e:
            if _is_billing_error(e):
                raise BillingProviderError(f"anthropic: billing/credit problem: {e}")
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
        return _response_from_message(message)


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


def _cost_usd(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
) -> float | None:
    """input_tokens from the API excludes cached tokens; the pieces are disjoint."""
    rates = PRICING.get(model_id)
    if rates is None:
        return None
    input_rate, output_rate = rates
    total = input_tokens * input_rate + output_tokens * output_rate
    total += (cache_write_tokens or 0) * input_rate * CACHE_WRITE_MULTIPLIER
    total += (cache_read_tokens or 0) * input_rate * CACHE_READ_MULTIPLIER
    return round(total / 1_000_000, 6)


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
            cache_read_tokens=response.cache_read_tokens if response else None,
            cache_write_tokens=response.cache_write_tokens if response else None,
            cost_usd=_cost_usd(
                model_id,
                response.input_tokens,
                response.output_tokens,
                response.cache_read_tokens,
                response.cache_write_tokens,
            )
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
        except BillingProviderError as e:
            _record(provider, model_id, started, "error:billing", error=e)
            raise  # no same-provider retry; the ladder escalates to fallback
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
        except BillingProviderError as e:
            _record(provider, model_id, started, "error:billing", error=e)
            raise
        _record(provider, model_id, started, "success", response=_usage_only(message))
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
    max_cost_usd: float = RUN_COST_BUDGET_USD,
    deadline_s: float = RUN_DEADLINE_S,
) -> ProviderResponse:
    """Drive the tool_use loop until the model stops calling tools.

    Per-run caps: a completed answer is never cut, but a runaway loop that
    keeps requesting tools past the cost budget or wall-clock deadline is
    stopped with RunLimitExceeded. Anthropic-only by design: the fallback
    provider runs tool-less (declared degradation).
    """
    messages: list[dict] = [{"role": "user", "content": user_content}]
    started_wall = time.monotonic()
    cost_so_far = 0.0
    last = None
    for turn in range(MAX_TOOL_TURNS + 1):
        message = _create_message_with_retry(
            provider, system, messages, model_id, max_tokens, temperature, tools
        )
        last = message
        cost_so_far += _cost_usd(
            model_id,
            message.usage.input_tokens,
            message.usage.output_tokens,
            message.usage.cache_read_input_tokens or 0,
            message.usage.cache_creation_input_tokens or 0,
        ) or 0.0
        if message.stop_reason != "tool_use":
            return _response_from_message(message)
        # More tool turns requested — enforce caps before spending another one.
        if cost_so_far > max_cost_usd:
            raise RunLimitExceeded(
                f"per-run cost budget ${max_cost_usd:.2f} exceeded "
                f"(spent ${cost_so_far:.2f}) after {turn + 1} tool turn(s)"
            )
        if time.monotonic() - started_wall > deadline_s:
            raise RunLimitExceeded(
                f"per-run deadline {deadline_s:.0f}s exceeded after {turn + 1} tool turn(s)"
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
    _warn_if_pricing_stale()
    try:
        primary = AnthropicProvider()
    except FatalProviderError as e:
        raise click.ClickException(str(e))
    primary_model = _resolve_model(model, primary.name)

    try:
        return _call_with_retry(primary, system, user_content, primary_model, max_tokens, temperature).text
    except FatalProviderError as e:
        raise click.ClickException(str(e))
    except BillingProviderError as e:
        click.echo(f"Warning: anthropic billing failed ({e}); trying fallback.", err=True)
        primary_error = e
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
    primary_error: Exception,
    tools_degraded: bool = False,
) -> str:
    """Climb the ladder: primary failed on a transient or billing error."""
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
    _warn_if_pricing_stale()
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
    except BillingProviderError as e:
        click.echo(f"Warning: anthropic billing failed ({e}); trying fallback.", err=True)
        primary_error = e
    except TransientProviderError as e:
        primary_error = e
    # RunLimitExceeded is intentionally NOT caught here: a per-run budget cut is
    # not a provider failure, so it propagates to the caller for a partial report.

    return _fallback_completion(
        system, user_content, model, max_tokens, temperature, primary_model, primary_error,
        tools_degraded=True,
    )
